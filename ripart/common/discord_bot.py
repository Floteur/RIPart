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

# --------------------------------------------------------------------------- #
# Provider metadata for rich embeds
# --------------------------------------------------------------------------- #
_PROVIDER_META: dict[str, dict[str, str]] = {
    "janitor": {
        "emoji": "🤖",
        "color": "blurple",
        "url": "https://janitorai.com",
        "description": "JanitorAI character extraction",
    },
    "saucepan": {
        "emoji": "🫕",
        "color": "gold",
        "url": "https://saucepan.ai",
        "description": "Saucepan companion extraction",
    },
    "clank": {
        "emoji": "⚙️",
        "color": "green",
        "url": "https://clank.world",
        "description": "clank.world character extraction",
    },
    "spicychat": {
        "emoji": "🌶️",
        "color": "red",
        "url": "https://spicychat.ai",
        "description": "SpicyChat character extraction",
    },
    "tavern": {
        "emoji": "🏰",
        "color": "teal",
        "url": "https://tavernai.net",
        "description": "TavernAI card extraction",
    },
    "chub": {
        "emoji": "📦",
        "color": "purple",
        "url": "https://chub.ai",
        "description": "Chub character extraction",
    },
    "misc": {
        "emoji": "📋",
        "color": "greyple",
        "url": "",
        "description": "Status and diagnostics",
    },
}


_color_map: dict[str, object] | None = None


def _provider_color(name: str) -> object:
    """Return a discord.Color for *name*.  Safe to call at module scope
    (discord is not imported at the top level).
    """
    global _color_map  # noqa: PLW0603
    import discord as _discord

    if _color_map is None:
        _color_map = {
            "blurple": _discord.Color.blurple(),
            "gold": _discord.Color.gold(),
            "green": _discord.Color.green(),
            "red": _discord.Color.red(),
            "teal": _discord.Color.teal(),
            "purple": _discord.Color.purple(),
            "greyple": _discord.Color.greyple(),
            "orange": _discord.Color.orange(),
        }
    meta = _PROVIDER_META.get(name, _PROVIDER_META["misc"])
    return _color_map.get(meta.get("color", "greyple"), _discord.Color.greyple())


# Buffers for streaming partial CLI output to the progress embed, keyed by user_id.
# Written by the worker thread, read by the asyncio timer task.
_progress_by_user: dict[int, "ProgressCapture"] = {}
_LINE_CLEAN = str.maketrans(
    "",
    "",
    "∙∘○●◎◉❖✦✧⟳⇄↻✓✗ℹ⚠⏳⌛▶❌✅➤→▸▪•·﹣━─│┃││░▒▓█▄▀■□▪▫▬▲▼◄►◆◇○◎●◐◑◒◓◔◕◖◗◦◘◙◚◛◜◝◞◟◠◡•",
)


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
    "help": "Show usage tips, examples, and provider info.",
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
    return (
        [*prefix, action, *parse_command(arguments)]
        if arguments.strip()
        else [*prefix, action]
    )


def _command_for(prefix: tuple[str, ...], action: str) -> click.Command:
    """Return the Click command mirrored by a slash-command action."""
    from ..cli import main

    command = main
    for part in (*prefix, action):
        candidate = command.commands.get(part)
        if candidate is None:
            raise RuntimeError(
                f"no Click command exists for {' '.join((*prefix, action))}"
            )
        command = candidate
    return command


def _discord_type(parameter: click.Parameter) -> object:
    """Translate the useful Click types into Discord's option types."""
    if isinstance(parameter.type, click.Choice):
        # Literal makes Discord render a picker rather than a free-form string.
        return Literal.__getitem__(
            tuple(str(choice) for choice in parameter.type.choices)
        )
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
                    description=_argument_description(
                        parameter, is_extract_uuid=is_extract_uuid
                    ),
                    required=parameter.required,
                    positional=True,
                )
            )
            continue
        if not isinstance(parameter, click.Option):
            continue
        # The first long option is the spelling users know from CLI help.
        flag = next(
            (item for item in parameter.opts if item.startswith("--")),
            parameter.opts[0],
        )
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
    console = Console(
        file=buffer, force_terminal=False, no_color=True, width=200, highlight=False
    )
    _capture.pair = {"out": console, "err": console}
    code = 0
    try:
        cli.main.main(args=list(argv), prog_name="rip", standalone_mode=False)
    except SystemExit as exc:
        code = (
            0 if exc.code in (0, None) else exc.code if isinstance(exc.code, int) else 1
        )
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
                        provider,
                        timeout,
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
        raise RuntimeError(
            "DISCORD_ADMIN_IDS must be comma-separated numeric Discord user IDs"
        ) from exc
    if not admin_ids:
        raise RuntimeError(
            "DISCORD_ADMIN_IDS must contain at least one Discord user ID"
        )
    return admin_ids


def _route_provider(url: str) -> str:
    """Map an ``extract <url>`` target to the provider lane that will handle it.

    Mirrors the URL routing in ``ripart.cli.extract`` so the queue serialises a
    job behind the same provider the CLI would actually use.
    """
    from ..providers import (
        chub as cb,
        clank as ck,
        saucepan as sp,
        spicychat as sc,
        tavern as tv,
    )

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
    logging.getLogger("discord").setLevel(
        logging.DEBUG if verbose > 1 else logging.INFO
    )
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.webhook").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(
        logging.INFO if verbose > 1 else logging.WARNING
    )
    # watchfiles's internal watcher logs at DEBUG per event — a flood.
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    logging.getLogger("watchdog").setLevel(logging.WARNING)


_ERROR_HINTS: list[tuple[tuple[str, ...], str]] = [
    (
        ("login", "log in", "authenticate", "unauthorized", "token"),
        "Run {login_cmd} first to authenticate.",
    ),
    (
        ("session", "cookie", "expired"),
        "Your session may have expired. Try {login_cmd} again.",
    ),
    (
        ("timeout", "timed out"),
        "The request timed out. Try again — if it persists, the provider may be slow.",
    ),
    (
        ("not found", "404", "no character"),
        "Double-check the URL or UUID — the character may have been deleted.",
    ),
    (
        ("rate limit", "429", "too many requests"),
        "Slow down! The provider is rate-limiting requests. Wait a moment and try again.",
    ),
    (
        ("cloudflare", "challenge"),
        "Cloudflare is blocking the request. Try running with `headed: true` or import a fresh session.",
    ),
]


def _error_hint(
    output: str, command_label: str, *, mention: Callable[..., str]
) -> str | None:
    """Return a human-friendly hint based on common error patterns.

    The hint is embedded in the result embed as an actionable suggestion.
    ``mention`` is a callable (e.g. ``client.cmd_mention``) used to render
    slash-command mentions in hint templates.
    """
    low = output.lower()
    provider = command_label.split()[0] if " " in command_label else None
    for keywords, hint in _ERROR_HINTS:
        if any(kw in low for kw in keywords):
            if "{login_cmd}" in hint and provider:
                hint = hint.replace("{login_cmd}", mention(provider, "login"))
            return hint
    if "error:" in low[:200] or "traceback" in low:
        return "An unexpected error occurred. Check the logs or run with `-v` for more details."
    return None


def _elapsed_pretty(seconds: float) -> str:
    """Human-readable elapsed time: ``1m 23s`` or ``45s``."""
    s = int(seconds)
    m, s = divmod(s, 60)
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _status_emoji(ok: bool, text: str = "") -> str:
    if "Timed out" in text:
        return "⚠️"
    return "✅" if ok else "❌"


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

    async for changes in watchfiles.awatch(ripart_root):
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


_LIST_ITEMS_PER_PAGE = 5


def _parse_list_output(provider: str, output: str) -> list[dict[str, str]]:
    """Parse a provider list CLI table output into structured items."""
    import re

    items: list[dict[str, str]] = []
    for line in output.strip().split("\n"):
        stripped = line.strip()
        if not stripped or set(stripped).issubset({"─", "━", "—", "-", " ", "═"}):
            continue
        cols = re.split(r"\s{2,}", stripped)
        if len(cols) < 3:
            continue
        if not cols[0].strip().isdigit():
            continue
        name = cols[1].strip()[:80]
        identifier = cols[-1].strip().rstrip("…")
        if name and identifier:
            if (
                provider == "janitor"
                and "/" not in identifier
                and "." not in identifier
            ):
                identifier = f"https://janitorai.com/characters/{identifier}"
            items.append({"name": name, "id": identifier})
    return items


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

    # ── Interactive list view ─────────────────────────────────────────────

    class ListView(discord.ui.View):
        """Paginated interactive list with per-character extract buttons."""

        def __init__(self, provider: str, items: list[dict], user_id: int) -> None:
            super().__init__(timeout=300)
            self.provider = provider
            self.items = items
            self.user_id = user_id
            self.page = 0
            self.selected_indices: set[int] = set()
            self._build_page()

        def _total_pages(self) -> int:
            return max(
                1, (len(self.items) + _LIST_ITEMS_PER_PAGE - 1) // _LIST_ITEMS_PER_PAGE
            )

        def _page_items(self) -> list[dict]:
            start = self.page * _LIST_ITEMS_PER_PAGE
            return self.items[start : start + _LIST_ITEMS_PER_PAGE]

        def _build_embed(self) -> discord.Embed:
            meta = _PROVIDER_META.get(self.provider, _PROVIDER_META["misc"])
            page_items = self._page_items()
            total = len(self.items)
            start = self.page * _LIST_ITEMS_PER_PAGE
            lines: list[str] = []
            for i, item in enumerate(page_items):
                num = start + i + 1
                marker = "●" if (start + i) in self.selected_indices else "○"
                lines.append(f"`{num:>3}.` {marker} **{item['name']}**")
            embed = discord.Embed(
                title=f"{meta['emoji']} {self.provider.title()} Characters",
                description="\n".join(lines) if lines else "No characters found.",
                color=_provider_color(self.provider),
            )
            sel = len(self.selected_indices)
            embed.set_footer(
                text=f"Page {self.page + 1}/{self._total_pages()} • {total} total • {sel} selected"
            )
            return embed

        def _build_page(self) -> None:
            self.clear_items()
            for i, item in enumerate(self._page_items()):
                global_idx = self.page * _LIST_ITEMS_PER_PAGE + i
                style = (
                    discord.ButtonStyle.primary
                    if global_idx in self.selected_indices
                    else discord.ButtonStyle.secondary
                )
                btn = discord.ui.Button(
                    label=item["name"][:80], style=style, custom_id=f"lv_sel_{i}"
                )
                btn.callback = self._make_select_cb(global_idx)
                self.add_item(btn)
            total = self._total_pages()
            prev = discord.ui.Button(
                label="◀",
                style=discord.ButtonStyle.gray,
                custom_id="lv_prev",
                disabled=self.page == 0,
            )
            prev.callback = self._prev_page
            self.add_item(prev)
            self.add_item(
                discord.ui.Button(
                    label=f"Page {self.page + 1}/{total}",
                    style=discord.ButtonStyle.gray,
                    custom_id="lv_page",
                    disabled=True,
                )
            )
            close = discord.ui.Button(
                label="✕", style=discord.ButtonStyle.red, custom_id="lv_close"
            )
            close.callback = self._close
            self.add_item(close)
            nxt = discord.ui.Button(
                label="▶",
                style=discord.ButtonStyle.gray,
                custom_id="lv_next",
                disabled=self.page >= total - 1,
            )
            nxt.callback = self._next_page
            self.add_item(nxt)
            sel = len(self.selected_indices)
            extract = discord.ui.Button(
                label=f"📥 Extract {sel}" if sel else "📥 Extract",
                style=discord.ButtonStyle.green if sel else discord.ButtonStyle.gray,
                custom_id="lv_extract",
                disabled=sel == 0,
            )
            extract.callback = self._extract
            self.add_item(extract)

        def _make_select_cb(self, global_idx: int):
            async def cb(interaction: discord.Interaction) -> None:
                if interaction.user.id != self.user_id:
                    await interaction.response.send_message(
                        "Not your list.", ephemeral=True
                    )
                    return
                self.selected_indices.symmetric_difference_update({global_idx})
                self._build_page()
                await interaction.response.edit_message(
                    embed=self._build_embed(), view=self
                )

            return cb

        async def _prev_page(self, interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "Not your list.", ephemeral=True
                )
                return
            self.page = max(0, self.page - 1)
            self._build_page()
            await interaction.response.edit_message(
                embed=self._build_embed(), view=self
            )

        async def _next_page(self, interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "Not your list.", ephemeral=True
                )
                return
            self.page = min(self._total_pages() - 1, self.page + 1)
            self._build_page()
            await interaction.response.edit_message(
                embed=self._build_embed(), view=self
            )

        async def _close(self, interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "Not your list.", ephemeral=True
                )
                return
            await interaction.response.edit_message(view=None)
            self.stop()

        async def _extract(self, interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "Not your list.", ephemeral=True
                )
                return
            if not self.selected_indices:
                await interaction.response.send_message(
                    "Select a character first.", ephemeral=True
                )
                return
            selected = sorted(self.selected_indices)
            if interaction.guild_id != guild_id or (
                channel_id is not None and interaction.channel_id != channel_id
            ):
                hint = ""
                ch = interaction.guild.get_channel(channel_id)
                if ch:
                    hint = f" Use <#{channel_id}>."
                await interaction.response.send_message(
                    f"⛔ Not available in this channel.{hint}", ephemeral=True
                )
                return
            label = f"{self.provider} extract"
            if not queue.reserve(self.user_id, label, self.provider):
                existing = queue.active_job(self.user_id)
                running = (
                    f"Currently running: `{existing.command_label}` on **{existing.provider}** lane.\n"
                    if existing
                    else ""
                )
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title=f"rip {label}",
                        description=f"⛔ Already running.\n{running}Only one at a time — wait for it to finish.",
                        color=discord.Color.red(),
                    ),
                    ephemeral=True,
                )
                return

            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)

            total = len(selected)
            results: list[tuple[int, JobResult]] = []
            _LOG.info(
                "list-extract %d selection(s) for %s (%s) [lane=%s]",
                total,
                interaction.user,
                self.user_id,
                self.provider,
            )
            started_at = time.monotonic()

            for seq, idx in enumerate(selected, start=1):
                item = self.items[idx]
                argv = [self.provider, "extract", item["id"]]
                item_label = f"{label} ({seq}/{total})"
                buf = ProgressCapture()
                _progress_by_user[self.user_id] = buf
                finished = asyncio.Event()
                refresh_tasks: list[asyncio.Task] = []
                status_msg: list[discord.Message | None] = [None]
                local_started: float | None = None

                async def on_queued(position: int) -> None:
                    meta = _PROVIDER_META.get(self.provider, _PROVIDER_META["misc"])
                    note = (
                        f"Extracting **{item['name']}**… {meta['emoji']} ({seq}/{total})"
                        if position == 1
                        else f"Queued **{item['name']}** — position {position} in the **{self.provider}** lane {meta['emoji']}"
                    )
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title=f"rip {item_label}",
                            description=f"⌛ {note}",
                            color=discord.Color.blurple(),
                        ),
                        ephemeral=True,
                    )

                async def on_start() -> None:
                    nonlocal local_started
                    local_started = time.monotonic()
                    finished.clear()
                    meta = _PROVIDER_META.get(self.provider, _PROVIDER_META["misc"])
                    embed = discord.Embed(
                        title=f"rip {item_label}",
                        description=f"▶ Running **{item['name']}** {meta['emoji']}",
                        color=discord.Color.gold(),
                    )
                    status_msg[0] = await interaction.followup.send(
                        embed=embed, ephemeral=True
                    )
                    refresh_tasks.append(
                        asyncio.create_task(
                            _list_timer(
                                status_msg,
                                item_label,
                                self.provider,
                                finished,
                                lambda: local_started or 0.0,
                                buf,
                            ),
                            name="ripart-list-timer",
                        )
                    )

                async def on_timeout() -> None:
                    with contextlib.suppress(Exception):
                        await interaction.followup.send(
                            embed=discord.Embed(
                                title=f"rip {item_label}",
                                description="⚠ Timed out — may still be finishing in the background.",
                                color=discord.Color.orange(),
                            ),
                            ephemeral=True,
                        )

                result: JobResult
                try:
                    result = await queue.run(
                        self.provider,
                        lambda: _run_command(argv, buffer=buf),
                        on_queued=on_queued,
                        on_start=on_start,
                        on_timeout=on_timeout,
                    )
                except Exception as exc:
                    _LOG.exception("list-extract `%s` crashed", item_label)
                    result = JobResult(False, f"internal error: {exc}")
                finally:
                    _progress_by_user.pop(self.user_id, None)
                    finished.set()
                    for t in refresh_tasks:
                        t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        for t in refresh_tasks:
                            await t

                results.append((idx, result))

            queue.release(self.user_id)
            duration = int(time.monotonic() - started_at)
            ok_count = sum(1 for _, r in results if r.ok)
            _LOG.info(
                "finished %d list-extract(s) for %s — %d/%d ok in %ds",
                total,
                interaction.user,
                ok_count,
                total,
                duration,
            )
            summary_embed = discord.Embed(
                title=f"rip {self.provider} extract ({total})",
                description=f"✅ {ok_count}/{total} extracted in {_elapsed_pretty(duration)}",
                color=discord.Color.green()
                if ok_count == total
                else discord.Color.gold(),
            )
            failure_lines: list[str] = []
            for idx, r in results:
                item = self.items[idx]
                if r.ok:
                    continue
                name = item["name"][:50]
                snippet = (r.text or "")[:_INLINE_OUTPUT_LIMIT]
                failure_lines.append(f"**{name}** — {snippet}")
            if failure_lines:
                summary_embed.add_field(
                    name="Failures",
                    value="\n".join(failure_lines[:_INLINE_OUTPUT_LIMIT]),
                    inline=False,
                )
            # Send summary to the user — the view may be gone, so use followup
            with contextlib.suppress(Exception):
                await interaction.followup.send(embed=summary_embed, ephemeral=True)

    async def _list_timer(
        msg_ref: list,
        label: str,
        provider: str,
        finished: asyncio.Event,
        started_at,
        buf: ProgressCapture,
    ) -> None:
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=3)
                return
            except asyncio.TimeoutError:
                pass
            elapsed = time.monotonic() - (started_at() or time.monotonic())
            embed = discord.Embed(
                title=f"rip {label}",
                description=f"▶ Running {_PROVIDER_META.get(provider, _PROVIDER_META['misc'])['emoji']} **{provider}**",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Elapsed", value=_elapsed_pretty(elapsed), inline=True)
            if buf and buf.step:
                embed.add_field(name="Step", value=f"`{buf.step}`", inline=True)
            with contextlib.suppress(Exception):
                msg = msg_ref[0]
                if msg is not None:
                    await msg.edit(embed=embed)

    async def dispatch(
        interaction,
        argv: list[str],
        *,
        command_label: str,
        provider: str,
        is_extraction: bool,
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
                f"⛔ This command isn't available in this channel.{hint}",
                ephemeral=True,
            )
            return
        title = f"rip {command_label}"
        user_id = interaction.user.id
        limited = is_extraction and user_id not in admin_ids
        if limited:
            # reserve() is the atomic check-and-claim: if it fails, the user
            # already holds their single extraction slot.
            if not queue.reserve(user_id, command_label, provider):
                existing = queue.active_job(user_id)
                running = (
                    f"Currently running: `{existing.command_label}` on **{existing.provider}** lane.\n"
                    if existing is not None
                    else ""
                )
                msg = (
                    "⛔ You already have a command running or queued.\n"
                    f"{running}"
                    "Only one at a time — wait for it to finish."
                )
                await interaction.response.send_message(
                    embed=_embed(title, msg, discord.Color.red()),
                    ephemeral=True,
                )
                return

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
            meta = _PROVIDER_META.get(provider, _PROVIDER_META["misc"])
            if position == 1:
                note = f"Starting now… {meta['emoji']}"
            else:
                note = f"Queued — position {position} in the **{provider}** lane {meta['emoji']}"
            await interaction.edit_original_response(
                embed=make_embed(f"⌛ {note}", discord.Color.blurple())
            )

        async def on_start() -> None:
            nonlocal started_at
            started_at = time.monotonic()
            finished.clear()
            meta = _PROVIDER_META.get(provider, _PROVIDER_META["misc"])
            await interaction.edit_original_response(
                embed=make_embed(
                    f"▶ Running {meta['emoji']} **{provider}**", discord.Color.gold()
                )
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
            """Tick the elapsed timer and show live progress while the job runs."""
            while not finished.is_set():
                try:
                    await asyncio.wait_for(finished.wait(), timeout=3)
                    return
                except asyncio.TimeoutError:
                    pass
                elapsed = time.monotonic() - started_at if started_at else 0
                fields: list[tuple[str, str]] = [("Elapsed", _elapsed_pretty(elapsed))]
                buf = _progress_by_user.get(user_id)
                if buf and buf.step:
                    fields.append(("Step", f"`{buf.step}`"))
                meta = _PROVIDER_META.get(provider, _PROVIDER_META["misc"])
                status = f"▶ Running {meta['emoji']} **{provider}**"
                with contextlib.suppress(Exception):
                    await interaction.edit_original_response(
                        embed=make_embed(status, discord.Color.gold(), fields)
                    )

        buf = ProgressCapture()
        _progress_by_user[user_id] = buf

        _LOG.info(
            "queued `%s` for %s (%s) [lane=%s]",
            command_label,
            interaction.user,
            user_id,
            provider,
        )
        result: JobResult
        try:
            await interaction.response.defer(thinking=True, ephemeral=not is_public)
            result = await queue.run(
                provider,
                lambda: _run_command(argv, buffer=buf),
                on_queued=on_queued,
                on_start=on_start,
                on_timeout=on_timeout,
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

        # List commands → interactive paginated view
        if result.ok and command_label.endswith(" list") and result.text:
            items = _parse_list_output(provider, result.text)
            if items:
                list_view = ListView(provider=provider, items=items, user_id=user_id)
                meta = _PROVIDER_META.get(provider, _PROVIDER_META["misc"])
                embed = discord.Embed(
                    title=f"{meta['emoji']} {provider.title()} Characters",
                    description=f"Found {len(items)} character(s). Select one and click **Extract**.",
                    color=_provider_color(provider),
                )
                try:
                    await interaction.edit_original_response(
                        embed=embed, view=list_view
                    )
                    return
                except Exception:
                    _LOG.warning(
                        "failed to edit list view for %s, falling back to text embed",
                        command_label,
                    )

        duration = int(time.monotonic() - started_at) if started_at else 0
        _LOG.info(
            "finished `%s` for %s — %s in %ds",
            command_label,
            interaction.user,
            "ok" if result.ok else "fail",
            duration,
        )
        emoji = _status_emoji(result.ok, result.text or "")
        status_text = f"{emoji} {'Completed' if result.ok else 'Failed'}"
        if "Timed out" in (result.text or ""):
            color = discord.Color.orange()
        else:
            color = discord.Color.green() if result.ok else discord.Color.red()
        output = result.text or "(no output)"
        long_output = len(output) > _INLINE_OUTPUT_LIMIT
        attachment = (
            discord.File(io.BytesIO(output.encode("utf-8")), filename="rip-output.txt")
            if long_output
            else None
        )

        meta = _PROVIDER_META.get(provider, _PROVIDER_META["misc"])
        provider_color = _provider_color(provider)
        result_color = provider_color if result.ok else color

        result_embed = _embed(
            title, status_text, result_color, [("Duration", _elapsed_pretty(duration))]
        )
        if is_extraction and provider != "misc":
            result_embed.add_field(
                name="Provider",
                value=f"{meta['emoji']} **{provider.title()}**",
                inline=True,
            )

        if command_label == "status":
            snap = queue.snapshot()
            result_embed.add_field(
                name="⏱️ Bot uptime", value=_fmt_uptime(time.monotonic() - bot_started)
            )
            result_embed.add_field(name="🚀 In flight", value=str(snap["in_flight"]))
            lanes = ", ".join(snap["busy_lanes"]) or "—"
            result_embed.add_field(name="🛤️ Busy lanes", value=lanes)
            if snap["busy_users"]:
                result_embed.add_field(
                    name="👥 Active users",
                    value="\n".join(snap["busy_users"][:5]),
                    inline=False,
                )
            result_embed.add_field(name="🛡️ Admins", value=str(len(admin_ids)))

        if not result.ok and output and output != "(no output)":
            hint = _error_hint(
                output, command_label, mention=interaction.client.cmd_mention
            )
            if hint:
                result_embed.add_field(name="💡 Suggestion", value=hint, inline=False)

        if long_output:
            result_embed.add_field(
                name="📄 Output",
                value="Attached as `rip-output.txt`.",
                inline=False,
            )
        elif output != "(no output)":
            # Truncate for embed limit (6 000 chars for description)
            snippet = output[:1_500]
            if len(output) > 1_500:
                snippet += "\n\n… (truncated)"
            result_embed.add_field(
                name="📄 Output", value=f"```\n{snippet}\n```", inline=False
            )

        async def _send_private(payload, **kw):
            try:
                await interaction.followup.send(**payload, **kw)
            except discord.HTTPException:
                _LOG.warning(
                    "failed to send private output to %s; falling back", user_id
                )
                try:
                    note = "*(private delivery failed; output shown below)*"
                    if is_public:
                        await interaction.edit_original_response(
                            embed=make_embed(
                                f"{status_text} — {note}",
                                discord.Color.orange(),
                                [("Duration", f"{duration}s")],
                            )
                        )
                    else:
                        await interaction.edit_original_response(
                            embed=_embed(
                                title,
                                f"{status_text}\n{note}",
                                color,
                                [("Duration", f"{duration}s")],
                            )
                        )
                except Exception:
                    pass

        if is_public:
            public_embed = make_embed(
                f"{emoji} **{provider.title()}** — {status_text}",
                result_color,
                [("Duration", _elapsed_pretty(duration))],
            )
            await interaction.edit_original_response(embed=public_embed)
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

    async def _help_command(interaction) -> None:
        """Show usage tips, examples, and provider info."""
        embed = discord.Embed(
            title="📚 RIPart — Quick Reference",
            description="Extract AI characters and lorebooks from multiple providers.",
            color=discord.Color.blurple(),
        )
        embed.set_author(
            name=interaction.user.display_name,
            icon_url=interaction.user.display_avatar.url,
        )

        # --- Getting started ---
        embed.add_field(
            name="🚀 Getting Started",
            value=(
                "1. Use `/rip <provider> login` to authenticate with a provider\n"
                "2. Use `/rip extract <url>` to extract a character\n"
                "3. Use `/rip status` to check auth state and queue"
            ),
            inline=False,
        )

        # --- Extract examples ---
        embed.add_field(
            name="🔍 Extraction Examples",
            value=(
                "`/rip extract <janitorai-url>` — JanitorAI\n"
                "`/rip extract <saucepan-url>` — Saucepan\n"
                "`/rip extract <clank-url>` — clank.world\n"
                "`/rip extract <spicychat-url>` — SpicyChat\n"
                "`/rip extract <tavern-url>` — TavernAI card\n"
                "`/rip extract <chub-url>` — Chub"
            ),
            inline=False,
        )

        # --- Providers ---
        provider_lines = []
        for name, meta in _PROVIDER_META.items():
            if name == "misc":
                continue
            provider_lines.append(
                f"{meta['emoji']} **{name.title()}** — {meta['description']}"
            )
        embed.add_field(
            name="🏷️ Providers",
            value="\n".join(provider_lines),
            inline=False,
        )

        # --- Tips ---
        embed.add_field(
            name="💡 Tips",
            value=(
                "• URLs are auto-detected — paste any supported URL\n"
                "• One extraction per person at a time\n"
                "• Use `/rip status` to see queue and auth info\n"
                "• Login sessions are saved locally"
            ),
            inline=False,
        )

        embed.set_footer(text="RIPart — free AI character extraction")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def add_action(
        parent, *, prefix: tuple[str, ...], action: str, description: str
    ) -> None:
        # Special-case: the `help` action is a Discord-only command with no CLI counterpart.
        if action == "help" and not prefix:
            parent.add_command(
                app_commands.Command(
                    name="help", description=description, callback=_help_command
                )
            )
            return

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
                inspect.Parameter(
                    "interaction", inspect.Parameter.POSITIONAL_OR_KEYWORD
                ),
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
            app_commands.Command(
                name=action, description=description, callback=callback
            )
        )

    for action, description in _ROOT_ACTIONS.items():
        add_action(rip_group, prefix=(), action=action, description=description)
    for provider, actions in _PROVIDER_ACTIONS.items():
        provider_group = app_commands.Group(
            name=provider, description=f"{provider} RIPart actions."
        )
        for action, description in actions.items():
            add_action(
                provider_group,
                prefix=(provider,),
                action=action,
                description=description,
            )
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
            self._cmd_mentions: dict[str, str] = {}

        def _build_cmd_map(self, synced: list) -> None:
            from discord.app_commands.models import AppCommandGroup as Acg

            def walk(opt, prefix):
                if not hasattr(opt, "options"):
                    return
                for sub in opt.options:
                    if isinstance(sub, Acg):
                        self._cmd_mentions[sub.qualified_name] = sub.mention
                        walk(sub, prefix)

            self._cmd_mentions.clear()
            for cmd in synced:
                self._cmd_mentions[cmd.name] = cmd.mention
                walk(cmd, cmd.name)

        def cmd_mention(self, *parts: str) -> str:
            path = " ".join(parts)
            return self._cmd_mentions.get(path, f"/rip {path}")

        async def _rebuild_commands(self) -> None:
            self.tree.clear_commands(guild=discord.Object(id=guild_id))
            self.tree.clear_commands(guild=None)
            _build_commands(self, guild_id, channel_id, admin_ids, queue, bot_started)
            synced = await self.tree.sync(guild=discord.Object(id=guild_id))
            self._build_cmd_map(synced)

        async def setup_hook(self) -> None:
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            _build_commands(self, guild_id, channel_id, admin_ids, queue, bot_started)
            synced = await self.tree.sync(guild=discord.Object(id=guild_id))
            self._build_cmd_map(synced)
            _LOG.info(
                "gateway ready as %s — guild %s, channel %s, %d admin(s); %d command(s) synced",
                self.user,
                guild_id,
                channel_id or "any",
                len(admin_ids),
                len(synced),
            )
            if self._reload:
                asyncio.create_task(
                    _hot_reload_watcher(self, self._ripart_root),
                    name="ripart-hot-reload",
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
