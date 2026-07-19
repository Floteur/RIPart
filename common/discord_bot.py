"""Guild-scoped Discord slash-command gateway for the RIPart CLI.

This module intentionally does not expose a shell.  Discord supplies a command
line which is parsed with :mod:`shlex` and executed as ``python -m ripart``;
therefore metacharacters, redirects, and command substitution never run.
Commands are processed by one FIFO worker so browser profiles and provider
sessions cannot be used concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import io
import inspect
import logging
import os
import shlex
import sys
import time
from typing import Any, Awaitable, Callable, Literal
from dataclasses import dataclass
from pathlib import Path

import click

from .discord_forum import _load_env

_ROOT = Path(__file__).resolve().parent.parent
_MAX_OUTPUT_BYTES = 1_000_000
_DEFAULT_TIMEOUT_SECONDS = 900
_LOG = logging.getLogger(__name__)

# Keep the Discord command picker aligned with every user-facing RIPart CLI
# command.  Its fields are generated from the Click parameters below, so
# Discord shows the same input names, types, and choices as the CLI.
_ROOT_ACTIONS: dict[str, str] = {
    "status": "Check the JanitorAI login state.",
    "login": "Open the JanitorAI login flow.",
    "import-session": "Import a JanitorAI session file.",
    "lorebook": "Index one JanitorAI lorebook.",
    "inspect": "Inspect one character.",
    "extract": "Extract one character or supported card URL.",
    "recent": "List recent JanitorAI characters.",
    "completion": "Print local shell-completion instructions.",
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
class CommandResult:
    """The bounded, combined output from one child CLI process."""

    argv: tuple[str, ...]
    returncode: int
    output: str
    truncated: bool


@dataclass(frozen=True)
class CommandProgress:
    """A lifecycle or output update from the serial CLI worker."""

    state: Literal["started", "output"]
    pid: int | None = None
    output: str = ""


ProgressCallback = Callable[[CommandProgress], Awaitable[None]]


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
        return "Character UUID from a list/search result."
    return {
        "path": "Local session-file path.",
        "query": "Search text.",
        "url": "Character URL or UUID.",
        "lorebook_id": "Lorebook ID.",
        "shell": "Shell name.",
    }.get(parameter.name, f"{parameter.name.replace('_', ' ')} input.")


def _option_description(flag: str) -> str:
    """Keep every option discoverable without duplicating lengthy CLI help."""
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
                    description=(
                        "Character UUID from a list/search result."
                        if is_extract_uuid
                        else _argument_description(parameter, is_extract_uuid=False)
                    ),
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
                description=_option_description(flag),
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


class SerialCommandRunner:
    """Run CLI child processes strictly one at a time in submission order."""

    def __init__(self, *, timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds
        self._queue: asyncio.Queue[
            tuple[list[str], asyncio.Future[CommandResult], ProgressCallback | None]
        ] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._active = False

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._work(), name="ripart-discord-cli")

    async def close(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None

    def enqueue(
        self, argv: list[str], *, progress: ProgressCallback | None = None
    ) -> tuple[int, asyncio.Future[CommandResult]]:
        """Queue work and return its one-based position plus completion future."""
        self.start()
        future: asyncio.Future[CommandResult] = asyncio.get_running_loop().create_future()
        position = self._queue.qsize() + (1 if self._active else 0) + 1
        self._queue.put_nowait((argv, future, progress))
        return position, future

    async def submit(self, argv: list[str]) -> CommandResult:
        """Queue one command and return its result once the worker reaches it."""
        _position, future = self.enqueue(argv)
        return await future

    async def _work(self) -> None:
        while True:
            argv, future, progress = await self._queue.get()
            self._active = True
            try:
                result = (
                    await self._run(argv, progress=progress)
                    if progress is not None
                    else await self._run(argv)
                )
                if not future.cancelled():
                    future.set_result(result)
            except Exception as exc:  # noqa: BLE001 - surface runner failure to caller
                if not future.cancelled():
                    future.set_exception(exc)
            finally:
                self._active = False
                self._queue.task_done()

    async def _notify(
        self, progress: ProgressCallback | None, update: CommandProgress
    ) -> None:
        """Send a best-effort UI update without letting Discord affect the CLI."""
        if progress is None:
            return
        try:
            await progress(update)
        except Exception:  # noqa: BLE001 - progress delivery must not abort work
            _LOG.warning("Unable to deliver Discord command progress", exc_info=True)

    async def _run(
        self, argv: list[str], *, progress: ProgressCallback | None = None
    ) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "ripart",
            *argv,
            cwd=_ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await self._notify(progress, CommandProgress("started", pid=process.pid))

        output_parts: list[bytes] = []
        output_size = 0
        retained_size = 0

        async def read_output() -> None:
            nonlocal output_size, retained_size
            assert process.stdout is not None
            while chunk := await process.stdout.read(4_096):
                output_size += len(chunk)
                if retained_size < _MAX_OUTPUT_BYTES:
                    remaining = _MAX_OUTPUT_BYTES - retained_size
                    retained = chunk[:remaining]
                    output_parts.append(retained)
                    retained_size += len(retained)
                await self._notify(
                    progress,
                    CommandProgress("output", output=chunk.decode("utf-8", errors="replace")),
                )

        output_task = asyncio.create_task(read_output(), name="ripart-discord-cli-output")
        try:
            await asyncio.wait_for(process.wait(), timeout=self.timeout_seconds)
            await output_task
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            await output_task
            return CommandResult(
                tuple(argv),
                124,
                f"command exceeded the {self.timeout_seconds}-second limit",
                False,
            )
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            await output_task
            raise
        output_bytes = b"".join(output_parts)
        truncated = output_size > _MAX_OUTPUT_BYTES
        output = output_bytes.decode("utf-8", errors="replace")
        return CommandResult(tuple(argv), process.returncode or 0, output, truncated)


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


def _result_preview(result: CommandResult) -> str:
    output = result.output.strip() or "(no output)"
    if len(output) > 1_500:
        output = "…\n" + output[-1_500:]
    status = "completed" if result.returncode == 0 else f"failed (exit {result.returncode})"
    suffix = "\nOutput was capped at 1 MiB." if result.truncated else ""
    return f"{status}: `rip {' '.join(result.argv)}`\n```\n{output}\n```{suffix}"


def _configure_logging(verbose: int) -> None:
    """Emit useful bot diagnostics without logging Discord payloads.

    discord.py's debug transport and gateway loggers include full API payloads.
    Those can contain account metadata and interaction contents, so verbosity is
    deliberately limited to lifecycle and command-queue diagnostics.
    """
    if not verbose:
        return
    logging.basicConfig(
        level=logging.DEBUG if verbose > 1 else logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)


def run_discord_bot(*, verbose: int = 0) -> None:
    """Start the configured guild's private, serial `/rip` command gateway."""
    _load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN must be set in .env")
    guild_id = _env_int("DISCORD_GUILD_ID", required=True)
    channel_id = _env_int("DISCORD_COMMAND_CHANNEL_ID")
    admin_ids = _admin_user_ids()
    timeout = int(os.environ.get("DISCORD_CLI_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS))
    if timeout < 1:
        raise RuntimeError("DISCORD_CLI_TIMEOUT_SECONDS must be positive")

    try:
        import discord
        from discord import app_commands
    except ImportError as exc:
        raise RuntimeError(
            "Discord command support is optional; install it with `uv sync --extra discord`."
        ) from exc

    _configure_logging(verbose)

    runner = SerialCommandRunner(timeout_seconds=timeout)
    guild = discord.Object(id=guild_id)

    class RipDiscordClient(discord.Client):
        def __init__(self) -> None:
            # Slash commands need guild state, and this standard intent is not
            # privileged.  It also prevents discord.py's misleading warning.
            intents = discord.Intents.none()
            intents.guilds = True
            super().__init__(intents=intents)
            self.tree = app_commands.CommandTree(self)

        async def setup_hook(self) -> None:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            runner.start()
            _LOG.info("Discord command gateway ready for guild %s", guild_id)

        async def close(self) -> None:
            await runner.close()
            await super().close()

    client = RipDiscordClient()

    async def dispatch(interaction, argv: list[str]) -> None:
        if interaction.guild_id != guild_id or (
            channel_id is not None and interaction.channel_id != channel_id
        ):
            await interaction.response.send_message("This command is not available here.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        started_at: float | None = None
        last_output_at: float | None = None
        pid: int | None = None
        output_tail = ""
        finished = asyncio.Event()
        private_progress = None

        async def on_progress(update: CommandProgress) -> None:
            nonlocal started_at, last_output_at, output_tail, pid
            if update.state == "started":
                started_at = time.monotonic()
                pid = update.pid
            elif update.output:
                last_output_at = time.monotonic()
                output_tail = (output_tail + update.output)[-1_500:]

        position, result_future = runner.enqueue(argv, progress=on_progress)
        _LOG.info("Discord command queued: %s (position %d)", " ".join(argv[:2]), position)
        command_name = " ".join(argv[:2])
        message = (
            f"⌛ RIPart command queued: `{command_name}` — starting now."
            if position == 1
            else f"⌛ RIPart command queued: `{command_name}` — position {position}."
        )
        await interaction.edit_original_response(content=message)
        private_progress = await interaction.followup.send(
            "Waiting for the serial worker…", ephemeral=True, wait=True
        )

        async def refresh_status() -> None:
            """Render process activity at a rate Discord comfortably accepts."""
            while not finished.is_set():
                if started_at is not None:
                    elapsed = int(time.monotonic() - started_at)
                    activity = (
                        f" · last CLI output {int(time.monotonic() - last_output_at)}s ago"
                        if last_output_at is not None
                        else ""
                    )
                    process_id = f" · PID {pid}" if pid is not None else ""
                    try:
                        await interaction.edit_original_response(
                            content=(
                                f"▶ RIPart command running: `{command_name}`{process_id}"
                                f" · {elapsed}s elapsed{activity}"
                            )
                        )
                        if output_tail and private_progress is not None:
                            tail = output_tail[-1_500:]
                            await private_progress.edit(
                                content=f"Live CLI output for `{command_name}`:\n```\n{tail}\n```"
                            )
                    except Exception:  # noqa: BLE001 - CLI work must stay independent of UI edits
                        _LOG.warning("Unable to refresh Discord command status", exc_info=True)
                try:
                    await asyncio.wait_for(finished.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass

        refresh_task = asyncio.create_task(refresh_status(), name="ripart-discord-status")
        try:
            result = await result_future
        finally:
            finished.set()
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
        _LOG.info(
            "Discord command finished: %s (exit %d)", " ".join(argv[:2]), result.returncode
        )
        elapsed = int(time.monotonic() - started_at) if started_at is not None else 0
        outcome = "completed" if result.returncode == 0 else f"failed (exit {result.returncode})"
        await interaction.edit_original_response(
            content=f"{'✅' if result.returncode == 0 else '❌'} RIPart command {outcome}: `{command_name}` · {elapsed}s"
        )
        if private_progress is not None:
            await private_progress.edit(content="CLI process finished; full result follows.")
        preview = _result_preview(result)
        attachment = None
        if len(result.output) > 1_500:
            attachment = discord.File(
                io.BytesIO(result.output.encode("utf-8")), filename="rip-output.txt"
            )
        followup_kwargs = {"ephemeral": True}
        if attachment is not None:
            followup_kwargs["file"] = attachment
        await interaction.followup.send(preview, **followup_kwargs)

    rip_group = app_commands.Group(
        name="rip", description="Run RIPart actions serially (one at a time)."
    )

    def add_action(parent, *, prefix: tuple[str, ...], action: str, description: str) -> None:
        fields = action_options(prefix, action)

        async def callback(interaction, **values) -> None:
            if not action_allowed(
                action=action, user_id=interaction.user.id, admin_ids=admin_ids
            ):
                await interaction.response.send_message(
                    "Only the designated bot owner can run logout actions.", ephemeral=True
                )
                return
            try:
                argv = action_argv_from_options(prefix, action, values)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await dispatch(interaction, argv)

        callback.__name__ = f"{('_'.join(prefix) or 'root')}_{action}".replace("-", "_")
        callback.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
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
        callback.__discord_app_commands_param_description__ = {  # type: ignore[attr-defined]
            field.name: field.description for field in fields
        }
        callback.__discord_app_commands_param_rename__ = {  # type: ignore[attr-defined]
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

    client.run(
        token,
        # Root logging above is the single handler.  Supplying a discord.py
        # handler as well duplicates every record.
        log_handler=None,
        log_level=logging.WARNING,
    )
