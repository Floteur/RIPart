"""Guild-scoped Discord slash-command gateway for the RIPart CLI.

Each slash command maps to a RIPart CLI command and runs **in-process** on a
worker thread (never a subprocess) via :class:`ExtractionQueue`. The queue keeps
one lock per provider, so jobs for the same provider run one at a time — sharing
the browser profile and the providers' module-level trace state safely — while
different providers extract in parallel. Anyone in the guild may submit, but
each person may have only one extraction queued or running at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib
import io
import inspect
import logging
import os
import pathlib
import shlex
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Literal
from dataclasses import dataclass

import click

from .env import load_env as _load_env

_INLINE_OUTPUT_LIMIT = 1_500  # longer results are sent as a file attachment instead
_DESCRIPTION_CAP = 50  # Discord option description max; keep short for 8 KiB tree limit
_LOG = logging.getLogger(__name__)

# Buffers for streaming partial CLI output to the progress embed, keyed by user_id.
# Written by the worker thread, read by the asyncio timer task.
_progress_by_user: dict[int, "ProgressCapture"] = {}
_LINE_CLEAN = str.maketrans("", "", "∙∘○●◎◉❖✦✧⟳⇄↻✓✗ℹ⚠⏳⌛▶❌✅➤→▸▪•·﹣━─│┃││░▒▓█▄▀■□▪▫▬▲▼◄►◆◇○◎●◐◑◒◓◔◕◖◗◦◘◙◚◛◜◝◞◟◠◡•")


class ProgressCapture(io.StringIO):
    """StringIO that pinpoints the most recent actionable CLI progress line."""

    def __init__(self) -> None:
        super().__init__()
        self.step: str = ""

    def write(self, s: str) -> int:
        result = super().write(s)
        line = s.strip()
        if not line or len(line) < 4:
            return result
        # Skip lines that are separators, JSON blocks, fences, or rich markup
        if line.startswith(("```", "──", "══", "━━", "━━", "—" * 3, "━" * 3)):
            return result
        if line.startswith(("{", "[", "]", "}", "(")):
            return result
        clean = line.translate(_LINE_CLEAN).strip()
        if clean and len(clean) >= 4:
            self.step = clean[:120]
        return result

# Keep the Discord command picker aligned with every user-facing RIPart CLI
# command.  Its fields are generated from the Click parameters below, so
# Discord shows the same input names, types, and choices as the CLI.
_ROOT_ACTIONS: dict[str, str] = {
    "extract": "Extract one character or supported card URL.",
    "status": "Show the auth state of every provider and the library.",
}
_PROVIDER_ACTIONS: dict[str, dict[str, str]] = {
    "janitor": {
        "status": "Check the JanitorAI login state.",
        "login": "Open the JanitorAI login flow.",
        "import-session": "Import a JanitorAI session file.",
        "lorebook": "Index one JanitorAI lorebook.",
        "inspect": "Inspect one character.",
        "extract": "Extract one JanitorAI character.",
        "list": "List recent JanitorAI characters.",
        "recent": "List recent JanitorAI characters.",
    },
    "saucepan": {
        "login": "Log in to Saucepan.",
        "status": "Check the Saucepan token.",
        "logout": "Forget the Saucepan token.",
        "list": "List Saucepan companions.",
        "providers": "List Saucepan model providers.",
        "extract": "Extract one Saucepan companion.",
    },
    "clank": {
        "login": "Log in to clank.world.",
        "status": "Check the clank.world session.",
        "logout": "Forget the clank.world session.",
        "list": "List clank.world characters.",
        "extract": "Extract one clank.world character.",
    },
    "spicychat": {
        "login": "Log in to spicychat.ai.",
        "status": "Check the spicychat.ai session.",
        "logout": "Forget the spicychat.ai session.",
        "search": "Search spicychat.ai characters.",
        "list": "List spicychat.ai characters.",
        "extract": "Extract one spicychat.ai character.",
    },
}


@dataclass(frozen=True)
class JobResult:
    """The captured outcome of one in-process CLI command."""

    ok: bool
    text: str


@dataclass(frozen=True)
class ActionOption:
    """One typed Discord field and its corresponding CLI argument."""

    name: str
    annotation: object
    description: str
    required: bool
    positional: bool
    flag: str | None = None
    negative_flag: str | None = None
    default: object | None = None
    count: bool = False


def parse_command(command: str) -> list[str]:
    """Parse one RIPart command without admitting a shell or bot recursion."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"invalid command quoting: {exc}") from exc
    if not argv:
        raise ValueError("provide a RIPart command, for example: `saucepan list`")
    if argv[0] == "discord-bot":
        raise ValueError("`discord-bot` cannot be run from the Discord bot")
    return argv


def action_argv(prefix: tuple[str, ...], action: str, arguments: str) -> list[str]:
    """Build a CLI argv from a selected slash-command action and its options."""
    return [*prefix, action, *parse_command(arguments)] if arguments.strip() else [*prefix, action]


def _command_for(prefix: tuple[str, ...], action: str) -> click.Command:
    """Return the Click command mirrored by a slash-command action."""
    from ..cli import main

    command = main
    for part in (*prefix, action):
        candidate = command.commands.get(part)
        if candidate is None:
            raise RuntimeError(f"no Click command exists for {' '.join((*prefix, action))}")
        command = candidate
    return command


def _discord_type(parameter: click.Parameter) -> object:
    """Translate the useful Click types into Discord's option types."""
    if isinstance(parameter.type, click.Choice):
        # Literal makes Discord render a picker rather than a free-form string.
        return Literal.__getitem__(tuple(str(choice) for choice in parameter.type.choices))
    if isinstance(parameter.type, click.types.IntParamType):
        return int
    if isinstance(parameter.type, click.types.FloatParamType):
        return float
    if isinstance(parameter, click.Option) and parameter.is_bool_flag:
        return bool
    return str


def _argument_description(parameter: click.Argument, *, is_extract_uuid: bool) -> str:
    """Use compact descriptions: Discord caps the complete command tree at 8 KiB."""
    if is_extract_uuid:
        return "Character UUID or full page URL (a pasted URL is parsed for you)."
    return {
        "path": "Local session-file path.",
        "query": "Search text.",
        "url": "Character URL or UUID.",
        "lorebook_id": "Lorebook ID.",
        "shell": "Shell name.",
    }.get(parameter.name, f"{parameter.name.replace('_', ' ')} input.")


def _option_description(parameter: click.Option, flag: str) -> str:
    help_text = (parameter.help or "").strip()
    if help_text:
        if len(help_text) <= _DESCRIPTION_CAP:
            return help_text
        cut = help_text.rfind(" ", 0, _DESCRIPTION_CAP - 1)
        return help_text[:cut].rstrip(",") if cut > 0 else help_text[:_DESCRIPTION_CAP]
    return f"CLI option: {flag}"


@functools.lru_cache(maxsize=None)
def action_options(prefix: tuple[str, ...], action: str) -> tuple[ActionOption, ...]:
    """Expose the Click action's arguments and options as typed Discord fields."""
    options: list[ActionOption] = []
    for parameter in _command_for(prefix, action).params:
        if isinstance(parameter, click.Argument):
            is_extract_uuid = action == "extract" and parameter.name == "url"
            options.append(
                ActionOption(
                    name="uuid" if is_extract_uuid else parameter.name,
                    annotation=str,
                    description=_argument_description(parameter, is_extract_uuid=is_extract_uuid),
                    required=parameter.required,
                    positional=True,
                )
            )
            continue
        if not isinstance(parameter, click.Option):
            continue
        # The first long option is the spelling users know from CLI help.
        flag = next((item for item in parameter.opts if item.startswith("--")), parameter.opts[0])
        negative_flag = next(
            (item for item in parameter.secondary_opts if item.startswith("--")), None
        )
        options.append(
            ActionOption(
                name=parameter.name,
                annotation=_discord_type(parameter),
                description=_option_description(parameter, flag),
                required=parameter.required,
                positional=False,
                flag=flag,
                negative_flag=negative_flag,
                default=parameter.default,
                count=parameter.count,
            )
        )
    return tuple(options)


def action_argv_from_options(
    prefix: tuple[str, ...], action: str, values: dict[str, Any]
) -> list[str]:
    """Convert typed Discord fields into the exact Click argv for an action."""
    argv = [*prefix, action]
    for option in action_options(prefix, action):
        value = values.get(option.name)
        if option.positional:
            if value is None and option.required:
                raise ValueError(f"{option.name} is required")
            if value is not None:
                argv.append(str(value))
            continue
        if value is None:
            continue
        if option.annotation is bool:
            if value == option.default:
                continue
            if value:
                argv.append(str(option.flag))
            elif option.negative_flag:
                argv.append(option.negative_flag)
            continue
        if option.count:
            argv.extend([str(option.flag)] * int(value))
            continue
        argv.extend((str(option.flag), str(value)))
    return argv


def action_allowed(*, action: str, user_id: int, admin_ids: set[int]) -> bool:
    """Apply action-specific ownership restrictions after general bot access."""
    return action != "logout" or user_id in admin_ids


# --------------------------------------------------------------------------- #
# In-process command execution
#
# Commands run as Python in a worker thread (never a subprocess), so several
# providers extract in parallel within one process. ``ripart.cli`` prints
# through module-level ``console``/``err_console``; we wrap those with a
# thread-local proxy so each worker thread captures its own output instead of
# interleaving on one shared stdout.
# --------------------------------------------------------------------------- #

_capture = threading.local()


class _ProxyConsole:
    """Route ``ripart.cli`` console output to this thread's capture buffer."""

    def __init__(self, base: object, stream: str) -> None:
        self._base = base
        self._stream = stream

    def _target(self) -> object:
        pair = getattr(_capture, "pair", None)
        return self._base if pair is None else pair[self._stream]

    def __getattr__(self, name: str) -> object:
        # ``print`` and any other Console attribute resolve against the target
        # active on the calling thread (base console outside a capture block).
        return getattr(self._target(), name)


def _install_console_proxies() -> None:
    """Make ``ripart.cli``'s consoles thread-local so parallel jobs don't mix."""
    import ripart.cli as cli

    if not isinstance(cli.console, _ProxyConsole):
        cli.console = _ProxyConsole(cli.console, "out")
        cli.err_console = _ProxyConsole(cli.err_console, "err")


def _run_command(argv: list[str], *, buffer: io.StringIO | None = None) -> JobResult:
    """Run one RIPart CLI command in-process, capturing its output as text."""
    import ripart.cli as cli
    from rich.console import Console

    if buffer is None:
        buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, no_color=True, width=100, highlight=False)
    _capture.pair = {"out": console, "err": console}
    code = 0
    try:
        cli.main.main(args=list(argv), prog_name="rip", standalone_mode=False)
    except SystemExit as exc:
        code = 0 if exc.code in (0, None) else exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # noqa: BLE001 - backstop; RipGroup already reports most errors
        code = 1
        console.print(f"error: {exc}")
    finally:
        _capture.pair = None
    text = buffer.getvalue().strip() or "(no output)"
    return JobResult(code == 0, text)


@dataclass
class ActiveJobInfo:
    user_id: int
    command_label: str
    provider: str


class ExtractionQueue:
    """Serialise commands per provider while running providers in parallel.

    Each provider has its own lock, so jobs for the same provider run one at a
    time (protecting the shared browser profile and the providers' module-level
    trace state) while different providers proceed concurrently. Extractions are
    limited to one in flight per person via :meth:`reserve`.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._counts: dict[str, int] = {}
        self._active_jobs: dict[int, ActiveJobInfo] = {}

    def reserve(self, user_id: int, command_label: str, provider: str) -> bool:
        """Claim this user's single extraction slot; ``False`` if already taken."""
        if user_id in self._active_jobs:
            return False
        self._active_jobs[user_id] = ActiveJobInfo(
            user_id=user_id, command_label=command_label, provider=provider
        )
        return True

    def active_job(self, user_id: int) -> ActiveJobInfo | None:
        return self._active_jobs.get(user_id)

    def release(self, user_id: int) -> None:
        self._active_jobs.pop(user_id, None)  # pyright: ignore[reportArgumentType]

    def snapshot(self) -> dict[str, object]:
        """Best-effort queue state for status reporting (no locking)."""
        busy_users = [
            f"<@{j.user_id}> {' '.join(j.command_label.split()[:2])}"
            for j in self._active_jobs.values()
        ]
        return {
            "in_flight": len(self._active_jobs),
            "busy_lanes": sorted(p for p, n in self._counts.items() if n > 0),
            "busy_users": busy_users,
        }

    def _lock(self, provider: str) -> asyncio.Lock:
        lock = self._locks.get(provider)
        if lock is None:
            lock = self._locks[provider] = asyncio.Lock()
        return lock

    async def run(
        self,
        provider: str,
        thunk: Callable[[], JobResult],
        *,
        timeout: float | None = 600,
        on_queued: Callable[[int], Awaitable[None]] | None = None,
        on_start: Callable[[], Awaitable[None]] | None = None,
        on_timeout: Callable[[], Awaitable[None]] | None = None,
    ) -> JobResult:
        """Run ``thunk`` in a worker thread, serialised behind ``provider``.

        ``on_queued`` is called with this job's 1-based position in the
        provider's line; ``on_start`` fires once it actually begins running.
        If the job exceeds ``timeout`` seconds, ``on_timeout`` is called
        (but the thread cannot be force-killed — caller is warned).
        """
        position = self._counts.get(provider, 0) + 1
        self._counts[provider] = position
        if on_queued is not None:
            await on_queued(position)
        try:
            async with self._lock(provider):
                if on_start is not None:
                    await on_start()
                task = asyncio.create_task(asyncio.to_thread(thunk))
                try:
                    return await asyncio.wait_for(task, timeout=timeout)
                except asyncio.TimeoutError:
                    if on_timeout is not None:
                        await on_timeout()
                    _LOG.warning(
                        "job for provider %s exceeded %ss timeout — "
                        "the worker thread continues but its result is discarded",
                        provider, timeout,
                    )
                    return JobResult(False, f"Timed out after {timeout}s.")
        finally:
            self._counts[provider] -= 1


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = [f"{d}d" for _ in (1,) if d]
    parts += [f"{h}h" for _ in (1,) if h]
    parts += [f"{m}m" for _ in (1,) if m]
    parts.append(f"{s}s")
    return " ".join(parts)


def _env_int(name: str, *, required: bool = False) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        if required:
            raise RuntimeError(f"{name} must be set in .env")
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a numeric Discord ID") from exc


def _admin_user_ids() -> set[int]:
    raw = os.environ.get("DISCORD_ADMIN_IDS", "")
    try:
        admin_ids = {int(value.strip()) for value in raw.split(",") if value.strip()}
    except ValueError as exc:
        raise RuntimeError("DISCORD_ADMIN_IDS must be comma-separated numeric Discord user IDs") from exc
    if not admin_ids:
        raise RuntimeError("DISCORD_ADMIN_IDS must contain at least one Discord user ID")
    return admin_ids


def _route_provider(url: str) -> str:
    """Map an ``extract <url>`` target to the provider lane that will handle it.

    Mirrors the URL routing in ``ripart.cli.extract`` so the queue serialises a
    job behind the same provider the CLI would actually use.
    """
    from ..providers import chub as cb, clank as ck, saucepan as sp, spicychat as sc, tavern as tv

    if ck.is_clank_url(url):
        return "clank"
    if sp.is_saucepan_url(url):
        return "saucepan"
    if sc.is_spicychat_url(url):
        return "spicychat"
    if tv.is_card_url(url):
        return "tavern"
    if cb.is_chub_url(url):
        return "chub"
    return "janitor"


def _configure_logging(verbose: int) -> None:
    """Set up readable bot diagnostics on by default, without logging payloads.

    Lifecycle and command-queue events are logged at INFO with no ``-v`` needed,
    so a normally-started bot is no longer silent. ``-v`` adds our own DEBUG
    detail; ``-vv`` also unmutes discord.py's client logger. discord.py's HTTP
    and gateway loggers include full API payloads (account metadata, interaction
    contents), so they stay at WARNING at every verbosity. The webhook logger is
    pinned too: its DEBUG lines print the interaction-token-bearing callback URL
    (a secret) on every message edit, including the 3s elapsed-timer refresh.
    The httpx/httpcore loggers (used by discord.py and the card publisher) are
    pinned too — their DEBUG stream is a per-request flood that dumps full
    response headers, including ``set-cookie``.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose > 1 else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    _LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("discord").setLevel(logging.DEBUG if verbose > 1 else logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.webhook").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.INFO if verbose > 1 else logging.WARNING)
    # watchfiles's internal watcher logs at DEBUG per event — a flood.
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    logging.getLogger("watchdog").setLevel(logging.WARNING)


_ERROR_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("login", "log in", "authenticate", "unauthorized", "token"), "Run `/rip <provider> login` first to authenticate."),
    (("session", "cookie", "expired"), "Your session may have expired. Try `/rip <provider> login` again."),
    (("timeout", "timed out"), "The request timed out. Try again — if it persists, the provider may be slow."),
    (("not found", "404", "no character"), "Double-check the URL or UUID — the character may have been deleted."),
    (("rate limit", "429", "too many requests"), "Slow down! The provider is rate-limiting requests. Wait a moment and try again."),
    (("cloudflare", "challenge"), "Cloudflare is blocking the request. Try running with `headed: true` or import a fresh session."),
]


def _error_hint(output: str, command_label: str) -> str | None:
    low = output.lower()
    for keywords, hint in _ERROR_HINTS:
        if any(kw in low for kw in keywords):
            return hint
    if "error:" in low[:200] or "traceback" in low:
        return "An unexpected error occurred. Check the logs or run with `-v` for more details."
    return None


_HOT_RELOAD_LOCK = asyncio.Lock()


async def _reload_modules_under(ripart_root: str, changed_paths: set[str]) -> None:
    """Reload changed ``ripart`` modules via `importlib.reload`, restarting from
    leaves so internal references are up to date before their importers see them."""
    # Collect modules that need reloading, keyed by depth (deepest first).
    candidates: dict[str, str] = {}
    for name, mod in list(sys.modules.items()):
        if not name.startswith("ripart."):
            continue
        if name == __name__:
            # discord_bot.py infrastructure stays; structural changes need restart.
            continue
        path = getattr(mod, "__file__", None)
        if path and path.startswith(ripart_root):
            candidates.setdefault(name, path)
    # Only reload modules whose source file actually changed, plus any module
    # whose transitive dependency changed (conservative: reload all ripart
    # modules when anything changes — fast enough for development).
    if not candidates:
        return
    for name in sorted(candidates, key=lambda n: n.count("."), reverse=True):
        try:
            importlib.reload(sys.modules[name])
        except Exception as exc:
            _LOG.warning("hot-reload: %s (%s)", name, exc)


async def _hot_reload_watcher(client, ripart_root: str) -> None:
    """Watch ``ripart/`` for ``.py`` changes and reload modules in-place."""
    import watchfiles

    async for changes in watchfiles.awatch(ripart_root, poll_intervals=1000):
        py_changed: set[str] = set()
        for _, path in changes:
            if path.endswith(".py"):
                py_changed.add(path)
        if not py_changed:
            continue
        async with _HOT_RELOAD_LOCK:
            # Don't reload while extractions are in flight.
            if any(client._queue._active_jobs):
                _LOG.info("hot-reload deferred — extractions in flight")
                continue
            await _reload_modules_under(ripart_root, py_changed)
            await client._rebuild_commands()
            _LOG.info("hot-reload — commands re-synced")


def _build_commands(
    client,
    guild_id: int,
    channel_id: int | None,
    admin_ids: set[int],
    queue: ExtractionQueue,
    bot_started: float,
) -> None:
    """Build the full /rip command tree on *client* using the passed-in state.

    This is kept as a module-level function so that hot-reload can call it again
    without restarting the Discord connection.  All state is passed explicitly
    so there are no stale closure references across reloads.
    """
    import discord
    from discord import app_commands

    guild = discord.Object(id=guild_id)

    def _embed(title: str, description: str, color, fields=()):
        embed = discord.Embed(title=title, description=description, color=color)
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=True)
        return embed

    async def dispatch(
        interaction, argv: list[str], *, command_label: str, provider: str, is_extraction: bool
    ) -> None:
        nonlocal bot_started
        if interaction.guild_id != guild_id or (
            channel_id is not None and interaction.channel_id != channel_id
        ):
            hint = ""
            if channel_id is not None:
                channel = interaction.guild.get_channel(channel_id)
                if channel:
                    hint = f" Use <#{channel_id}>."
            await interaction.response.send_message(
                f"⛔ This command isn't available in this channel.{hint}", ephemeral=True
            )
            return
        title = f"rip {command_label}"
        user_id = interaction.user.id
        limited = is_extraction and user_id not in admin_ids
        if limited:
            existing = queue.active_job(user_id)
            if existing is not None:
                msg = (
                    "⛔ You already have a command running or queued.\n"
                    f"Currently running: `{existing.command_label}` on **{existing.provider}** lane.\n"
                    "Only one at a time — wait for it to finish."
                )
                await interaction.response.send_message(
                    embed=_embed(title, msg, discord.Color.red()), ephemeral=True,
                )
                return
            queue.reserve(user_id, command_label, provider)

        is_public = is_extraction

        def make_embed(description: str, color, fields=()):
            embed = _embed(title, description, color, fields)
            embed.set_author(
                name=interaction.user.display_name,
                icon_url=interaction.user.display_avatar.url,
            )
            if provider not in ("misc",) and is_public:
                embed.set_footer(text=f"lane: {provider}")
            return embed

        started_at: float | None = None
        finished = asyncio.Event()

        async def on_queued(position: int) -> None:
            note = (
                "Starting now…" if position == 1
                else f"Queued — position {position} in the **{provider}** lane."
            )
            await interaction.edit_original_response(
                embed=make_embed(f"⌛ {note}", discord.Color.blurple())
            )

        async def on_start() -> None:
            nonlocal started_at
            started_at = time.monotonic()
            finished.clear()
            await interaction.edit_original_response(
                embed=make_embed("▶ Running", discord.Color.gold())
            )
            refresh_tasks.append(
                asyncio.create_task(refresh_status(), name="ripart-discord-status")
            )

        async def on_timeout() -> None:
            await interaction.edit_original_response(
                embed=make_embed(
                    "⚠ Timed out — the extraction may still be finishing in the background.",
                    discord.Color.orange(),
                )
            )

        refresh_tasks: list[asyncio.Task] = []

        async def refresh_status() -> None:
            while not finished.is_set():
                try:
                    await asyncio.wait_for(finished.wait(), timeout=3)
                    return
                except asyncio.TimeoutError:
                    pass
                elapsed = int(time.monotonic() - started_at) if started_at else 0
                fields = [("Elapsed", f"{elapsed}s")]
                buf = _progress_by_user.get(user_id)
                if buf and buf.step:
                    fields.append(("Step", buf.step))
                with contextlib.suppress(Exception):
                    await interaction.edit_original_response(
                        embed=make_embed("▶ Running", discord.Color.gold(), fields)
                    )

        buf = ProgressCapture()
        _progress_by_user[user_id] = buf

        _LOG.info(
            "queued `%s` for %s (%s) [lane=%s]",
            command_label, interaction.user, user_id, provider,
        )
        result: JobResult
        try:
            await interaction.response.defer(thinking=True, ephemeral=not is_public)
            result = await queue.run(
                provider, lambda: _run_command(argv, buffer=buf),
                on_queued=on_queued, on_start=on_start, on_timeout=on_timeout,
            )
        except Exception as exc:
            _LOG.exception("job `%s` crashed", command_label)
            result = JobResult(False, f"internal error: {exc}")
        finally:
            _progress_by_user.pop(user_id, None)
            finished.set()
            for task in refresh_tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if limited:
                queue.release(user_id)

        duration = int(time.monotonic() - started_at) if started_at else 0
        _LOG.info(
            "finished `%s` for %s — %s in %ds",
            command_label, interaction.user, "ok" if result.ok else "fail", duration,
        )
        status_text = "✅ Completed" if result.ok else "❌ Failed"
        color = discord.Color.green() if result.ok else discord.Color.gold() if "Timeout" in (result.text or "") else discord.Color.red()
        output = result.text or "(no output)"
        long_output = len(output) > _INLINE_OUTPUT_LIMIT
        attachment = (
            discord.File(io.BytesIO(output.encode("utf-8")), filename="rip-output.txt")
            if long_output else None
        )

        result_embed = _embed(title, status_text, color, [("Duration", f"{duration}s")])
        if command_label == "status":
            snap = queue.snapshot()
            result_embed.add_field(name="Bot uptime", value=_fmt_uptime(time.monotonic() - bot_started))
            result_embed.add_field(name="In flight", value=str(snap["in_flight"]))
            result_embed.add_field(name="Busy lanes", value=", ".join(snap["busy_lanes"]) or "idle")
            if snap["busy_users"]:
                result_embed.add_field(name="Active users", value="\n".join(snap["busy_users"][:5]), inline=False)
            result_embed.add_field(name="Admins", value=str(len(admin_ids)))
        if not result.ok and output and output != "(no output)":
            hint = _error_hint(output, command_label)
            if hint:
                result_embed.add_field(name="💡 Hint", value=hint, inline=False)

        if long_output:
            result_embed.add_field(name="Output", value="Attached as `rip-output.txt`.", inline=False)
        elif output != "(no output)":
            result_embed.description = f"{status_text}\n```\n{output}\n```"

        async def _send_private(payload, **kw):
            try:
                await interaction.followup.send(**payload, **kw)
            except discord.HTTPException:
                _LOG.warning("failed to send private output to %s; falling back", user_id)
                try:
                    note = "*(private delivery failed; output shown below)*"
                    if is_public:
                        await interaction.edit_original_response(
                            embed=make_embed(f"{status_text} — {note}", discord.Color.orange(), [("Duration", f"{duration}s")])
                        )
                    else:
                        await interaction.edit_original_response(
                            embed=_embed(title, f"{status_text}\n{note}", color, [("Duration", f"{duration}s")])
                        )
                except Exception:
                    pass

        if is_public:
            await interaction.edit_original_response(
                embed=make_embed(status_text, color, [("Duration", f"{duration}s")])
            )
            if attachment is not None:
                await _send_private({"embed": result_embed, "file": attachment})
            else:
                await _send_private({"embed": result_embed})
        else:
            await interaction.edit_original_response(embed=result_embed)
            if attachment is not None:
                await _send_private({"file": attachment})

    rip_group = app_commands.Group(
        name="rip", description="Queue RIPart actions — providers run in parallel."
    )

    def add_action(parent, *, prefix: tuple[str, ...], action: str, description: str) -> None:
        fields = action_options(prefix, action)
        is_extraction = action == "extract"

        async def callback(interaction, **values) -> None:
            if not action_allowed(
                action=action, user_id=interaction.user.id, admin_ids=admin_ids
            ):
                await interaction.response.send_message(
                    "⛔ Logout is restricted to bot admins only.\n"
                    "Ask an admin to run this command.",
                    ephemeral=True,
                )
                return
            try:
                argv = action_argv_from_options(prefix, action, values)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            if prefix:
                provider = prefix[0]
            elif is_extraction:
                provider = _route_provider(str(values.get("uuid") or ""))
            else:
                provider = "misc"
            await dispatch(
                interaction,
                argv,
                command_label=" ".join((*prefix, action)),
                provider=provider,
                is_extraction=is_extraction,
            )

        callback.__name__ = f"{('_'.join(prefix) or 'root')}_{action}".replace("-", "_")
        callback.__signature__ = inspect.Signature(
            [
                inspect.Parameter("interaction", inspect.Parameter.POSITIONAL_OR_KEYWORD),
                *[
                    inspect.Parameter(
                        field.name,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        annotation=field.annotation,
                        default=inspect.Parameter.empty if field.required else None,
                    )
                    for field in fields
                ],
            ]
        )
        callback.__discord_app_commands_param_description__ = {
            field.name: field.description for field in fields
        }
        callback.__discord_app_commands_param_rename__ = {
            field.name: field.name.replace("_", "-") for field in fields
        }
        parent.add_command(
            app_commands.Command(name=action, description=description, callback=callback)
        )

    for action, description in _ROOT_ACTIONS.items():
        add_action(rip_group, prefix=(), action=action, description=description)
    for provider, actions in _PROVIDER_ACTIONS.items():
        provider_group = app_commands.Group(
            name=provider, description=f"{provider} RIPart actions."
        )
        for action, description in actions.items():
            add_action(provider_group, prefix=(provider,), action=action, description=description)
        rip_group.add_command(provider_group)
    client.tree.add_command(rip_group, guild=guild)


def run_discord_bot(*, verbose: int = 0, reload: bool = False) -> None:
    """Start the configured guild's private, serial `/rip` command gateway."""
    _load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN must be set in .env")
    guild_id = _env_int("DISCORD_GUILD_ID", required=True)
    channel_id = _env_int("DISCORD_COMMAND_CHANNEL_ID")
    admin_ids = _admin_user_ids()

    try:
        import discord
        from discord import app_commands
    except ImportError as exc:
        raise RuntimeError(
            "Discord command support is optional; install it with `uv sync --extra discord`."
        ) from exc

    _configure_logging(verbose)
    _install_console_proxies()

    queue = ExtractionQueue()
    bot_started = time.monotonic()

    ripart_root = str(pathlib.Path(__file__).resolve().parent.parent)

    if reload:
        try:
            import watchfiles  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "--reload needs watchfiles — install it with `uv sync --extra discord`"
            )

    class RipDiscordClient(discord.Client):
        def __init__(self) -> None:
            intents = discord.Intents.none()
            intents.guilds = True
            super().__init__(intents=intents)
            self.tree = app_commands.CommandTree(self)
            self._guild_id = guild_id
            self._channel_id = channel_id
            self._admin_ids = admin_ids
            self._queue = queue
            self._bot_started = bot_started
            self._reload = reload
            self._ripart_root = ripart_root

        async def _rebuild_commands(self) -> None:
            self.tree.clear_commands(guild=discord.Object(id=guild_id))
            self.tree.clear_commands(guild=None)
            _build_commands(self, guild_id, channel_id, admin_ids, queue, bot_started)
            await self.tree.sync(guild=discord.Object(id=guild_id))

        async def setup_hook(self) -> None:
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            _build_commands(self, guild_id, channel_id, admin_ids, queue, bot_started)
            synced = await self.tree.sync(guild=discord.Object(id=guild_id))
            _LOG.info(
                "gateway ready as %s — guild %s, channel %s, %d admin(s); %d command(s) synced",
                self.user, guild_id, channel_id or "any", len(admin_ids), len(synced),
            )
            if self._reload:
                asyncio.create_task(
                    _hot_reload_watcher(self, self._ripart_root), name="ripart-hot-reload"
                )
                _LOG.info(
                    "hot-reload on (watchfiles) — saved edits rebuild commands and reload providers; "
                    "restart for changes to discord_bot.py"
                )

    client = RipDiscordClient()

    client.run(
        token,
        log_handler=None,
        log_level=logging.WARNING,
    )
