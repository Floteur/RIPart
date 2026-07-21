"""Command-line interface for RIPart.

Thin, user-facing layer: it parses arguments, calls the Botasaurus tasks in
``browser_tasks``, and prints friendly results. All the real work happens in
``browser_tasks`` and ``helpers``.

Run ``rip --help`` (or ``python -m ripart --help``) to get started.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import rich_click as click
from rich.console import Console
from rich.table import Table

from . import cli_extractors
from .common.cards import save_to_library
from .common.discord_forum import publish_saved_card
from .common.lorebooks import update_lorebook_library
from .common.storage import state_path
from .common.text import safe_name, write_json
from .providers import chub as cb
from .providers import clank as ck
from .providers import saucepan as sp
from .providers import spicychat as sc
from .providers import tavern as tv
from .providers.janitor import (
    extract_task,
    import_session_task,
    inspect_task,
    login_task,
    lorebook_task,
    recent_task,
    status_task,
)

# --------------------------------------------------------------------------- #
# Paths & shared console
# --------------------------------------------------------------------------- #

# Keep everything self-contained under this project directory.
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output" / "cli"

console = Console()
err_console = Console(stderr=True)


# --------------------------------------------------------------------------- #
# Beautiful help configuration (rich-click)
# --------------------------------------------------------------------------- #

click.rich_click.TEXT_MARKUP = "rich"
click.rich_click.SHOW_ARGUMENTS = True
click.rich_click.GROUP_ARGUMENTS_OPTIONS = False
click.rich_click.OPTIONS_TABLE_COLUMN_TYPES = [
    "required", "opt_short", "opt_long", "metavar", "help"
]
click.rich_click.OPTIONS_TABLE_HELP_SECTIONS = [
    "help", "deprecated", "envvar", "default", "required"
]
click.rich_click.STYLE_OPTION = "bold cyan"
click.rich_click.STYLE_ARGUMENT = "bold cyan"
click.rich_click.STYLE_COMMAND = "bold green"
click.rich_click.STYLE_OPTIONS_TABLE_BOX = "SIMPLE"
click.rich_click.STYLE_COMMANDS_TABLE_BOX = "SIMPLE"
click.rich_click.MAX_WIDTH = 100

# Group the sub-commands into tidy, labelled panels in `rip --help`.
click.rich_click.COMMAND_GROUPS = {
    "rip": [
        {"name": "JanitorAI", "commands": ["janitor"]},
        {"name": "Open-card URL routing", "commands": ["extract"]},
        {"name": "Saucepan", "commands": ["saucepan"]},
        {"name": "Clank", "commands": ["clank"]},
        {"name": "Spicychat", "commands": ["spicychat"]},
        {"name": "Setup", "commands": ["completion"]},
    ],
    "rip saucepan": [
        {"name": "Session & login", "commands": ["login", "status", "logout"]},
        {"name": "Ripping", "commands": ["list", "extract", "providers"]},
    ],
    "rip clank": [
        {"name": "Session & login", "commands": ["login", "status", "logout"]},
        {"name": "Ripping", "commands": ["list", "extract"]},
    ],
    "rip spicychat": [
        {"name": "Session & login", "commands": ["login", "status", "logout"]},
        {"name": "Ripping", "commands": ["search", "list", "extract"]},
    ],
}


# --------------------------------------------------------------------------- #
# Small output helpers
# --------------------------------------------------------------------------- #


def _ok(message: str) -> None:
    console.print(f"[bold green]✓[/] {message}")


def _no(message: str) -> None:
    console.print(f"[bold red]✗[/] {message}")


def _field(label: str, value: object) -> None:
    console.print(f"  [dim]{label}:[/] {value}")


def _path(label: str, value: object) -> None:
    console.print(f"  [dim]{label}:[/] [cyan]{value}[/]")


def _extraction_ui() -> cli_extractors.ExtractionUI:
    """Bind provider extraction workflows to this command module's UI."""
    return cli_extractors.ExtractionUI(
        library_dir=OUT / "library",
        print=console.print,
        error=err_console.print,
        ok=_ok,
        no=_no,
        field=_field,
        path=_path,
        duration=_fmt_duration,
    )


def _library_has_card(
    library_dir: Path, character_id: object, *, source_url: str = ""
) -> bool:
    """Whether a provider item already has a saved library card.

    The library is UUID-keyed, so checking the card itself avoids a duplicate
    network fetch and also works when the optional index is absent or stale.
    """
    identifier = str(character_id or "").strip()
    if identifier and (library_dir / f"{identifier}.png").is_file():
        return True
    if not source_url:
        return False
    try:
        index = json.loads((library_dir / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(index, dict):
        return False
    return any(
        isinstance(entry, dict)
        and entry.get("url") == source_url
        and (library_dir / str(entry.get("file") or "")).is_file()
        for entry in index.values()
    )


# --------------------------------------------------------------------------- #
# Shared options
# --------------------------------------------------------------------------- #


def headed_option(func):
    """Add the shared ``--headed`` debug flag to a command."""
    return click.option(
        "--headed",
        is_flag=True,
        default=False,
        help="Debug escape hatch: open a visible browser instead of the default headless one.",
    )(func)


def verbose_option(func):
    """Add the shared repeatable ``-v``/``--verbose`` flag to a command.

    Stacks like a normal CLI verbosity flag: ``-v`` = progress diagnostics
    (chat/persona/trigger-pass narration, the classic --verbose), ``-vv`` = also
    one line per HTTP/generateAlpha call (status + timing), ``-vvv`` = also a
    truncated preview of each request/response payload — for digging all the
    way down into a bug.
    """
    return click.option(
        "-v",
        "--verbose",
        count=True,
        help="Repeatable: -v progress, -vv +wire-level call summaries, "
        "-vvv +raw request/response payload previews.",
    )(func)


def discord_verbose_option(func):
    """Add safe bot verbosity without exposing Discord API payloads."""
    return click.option(
        "-v",
        "--verbose",
        count=True,
        help="Repeatable: show Discord gateway lifecycle and command-queue diagnostics. "
        "Discord API payloads are never logged.",
    )(func)


def browser_kwargs(headed: bool) -> dict[str, bool]:
    return {"headless": not headed, "enable_xvfb_virtual_display": False}


# --------------------------------------------------------------------------- #
# Root group
# --------------------------------------------------------------------------- #


class RipGroup(click.RichGroup):
    """Group that turns unexpected errors into a clean one-line message.

    Set ``RIP_DEBUG=1`` to see the full traceback instead.
    """

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except (click.ClickException, click.Abort, click.exceptions.Exit, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately broad, user-facing
            if os.environ.get("RIP_DEBUG"):
                raise
            err_console.print(f"[bold red]error:[/] {exc}")
            err_console.print("[dim]set RIP_DEBUG=1 for the full traceback[/]")
            raise SystemExit(1)


@click.group(
    cls=RipGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(
    package_name="ripart", prog_name="rip", message="%(prog)s %(version)s"
)
def main() -> None:
    """[bold]RIPart[/] - rip characters & lorebooks from JanitorAI, Saucepan & clank.world.

    JanitorAI is browser-driven (powered by Botasaurus); Saucepan and clank.world
    use their native APIs. Typical JanitorAI flow:

    \b
      1. rip janitor login             log in once (reused afterwards)
      2. rip janitor status            confirm you are logged in
      3. rip janitor inspect <url>     peek at a character's public metadata
      4. rip janitor extract <url>     rip the full card + lorebook

    [bold]rip extract <url>[/] routes open-card URLs to their matching extractor.
    Use each provider group for authenticated extraction. Results are written
    under [cyan]output/cli/[/]. Run [bold]rip COMMAND --help[/] for details.
    """


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


@main.command()
@headed_option
def status(headed: bool) -> None:
    """Check whether the browser profile is currently logged in."""
    result = status_task({}, **browser_kwargs(headed))
    if result.get("loggedIn"):
        _ok("logged in")
        sys.exit(0)
    _no("not logged in - run [bold]rip janitor login[/] first")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--timeout",
    type=int,
    default=180,
    show_default=True,
    metavar="SECONDS",
    help="How long to wait for you to finish logging in.",
)
@click.option(
    "--headless",
    is_flag=True,
    default=False,
    help="Advanced: run without a visible window (only useful if the profile is already logged in).",
)
def login(timeout: int, headless: bool) -> None:
    """Open JanitorAI and wait for an authenticated session.

    A browser window opens on JanitorAI's login page. Sign in normally; once
    JanitorAI reports you as authenticated the session is saved to the profile
    and reused by every other command.
    """
    result = login_task(
        {"timeout": timeout},
        headless=headless,
        enable_xvfb_virtual_display=False,
    )
    if result.get("loggedIn"):
        _ok("logged in")
        if result.get("sessionSaved"):
            _path("session saved", result.get("sessionFile"))
        sys.exit(0)
    _no("login timed out - try again, or increase [bold]--timeout[/]")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# import-session
# --------------------------------------------------------------------------- #


@main.command(name="import-session")
@click.argument(
    "path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--refresh-wait",
    type=int,
    default=3,
    show_default=True,
    metavar="SECONDS",
    help="Seconds to let JanitorAI refresh the imported auth token.",
)
@click.option(
    "--check-timeout",
    type=int,
    default=0,
    show_default=True,
    metavar="SECONDS",
    help="Optionally keep probing the login state for this long after import.",
)
@click.option(
    "--bypass-cloudflare",
    is_flag=True,
    default=False,
    help="Try Botasaurus' Cloudflare-bypass helper if a challenge blocks the import.",
)
@verbose_option
@headed_option
def import_session(
    path: Path,
    refresh_wait: int,
    check_timeout: int,
    bypass_cloudflare: bool,
    verbose: int,
    headed: bool,
) -> None:
    """Import a local cookie / localStorage dump into the profile.

    PATH is a JSON session dump exported from a logged-in browser. Use this when
    you cannot log in interactively (e.g. on a headless server).
    """
    result = import_session_task(
        {
            "session_path": str(path.resolve()),
            "refresh_wait": refresh_wait,
            "check_timeout": check_timeout,
            "verbose": verbose,
            "bypass_cloudflare": bypass_cloudflare,
        },
        **browser_kwargs(headed),
    )
    _field("cookies imported", result.get("cookiesImported", 0))
    _field("auth cookies imported", result.get("authCookiesImported", 0))
    _field("localStorage keys imported", result.get("localStorageImported", 0))

    probe = result.get("probe") or {}
    if result.get("loggedIn"):
        _ok("logged in")
    else:
        _no("not logged in")
        if probe.get("status"):
            _field("login probe HTTP", probe["status"])
        if probe.get("cloudflare"):
            _no(
                "blocked by Cloudflare challenge - retry with [bold]--bypass-cloudflare[/]"
            )

    if verbose:
        _print_import_diagnostics(result.get("diagnostics") or {}, probe)

    sys.exit(0 if result.get("loggedIn") else 1)


def _print_import_diagnostics(diagnostics: dict, probe: dict | None = None) -> None:
    session = diagnostics.get("session") or {}
    browser = diagnostics.get("browser") or {}
    cookie_jar = diagnostics.get("cookieJar") or {}
    console.print("\n[bold]verbose diagnostics[/]")
    if session.get("nowUtc"):
        _field("now UTC", session["nowUtc"])
    if probe:
        _field(
            "login probe",
            f"status={probe.get('status', 0)} loggedIn={probe.get('loggedIn', False)} "
            f"cloudflare={probe.get('cloudflare', False)} challenge={probe.get('challenge', False)} "
            f"bodyLength={probe.get('bodyLength', 0)}"
            + (f" error={probe['error']}" if probe.get("error") else ""),
        )
    if diagnostics.get("bypassError"):
        _field("Cloudflare bypass attempt", diagnostics["bypassError"])
    if session.get("cookies"):
        console.print("  [dim]session cookies:[/]")
        for cookie in session["cookies"]:
            flags = [
                flag
                for flag, present in (
                    ("hostOnly", cookie.get("hostOnly")),
                    ("secure", cookie.get("secure")),
                    ("httpOnly", cookie.get("httpOnly")),
                    ("expired", cookie.get("expired")),
                )
                if present
            ]
            suffix = f" ({', '.join(flags)})" if flags else ""
            expires = cookie.get("expiresUtc") or "session"
            console.print(
                f"    - {cookie.get('name')} domain={cookie.get('domain')} "
                f"sameSite={cookie.get('sameSite')} expires={expires} "
                f"valueLength={cookie.get('valueLength')}{suffix}"
            )
    if session.get("auth"):
        console.print("  [dim]auth chunks:[/]")
        for auth in session["auth"]:
            console.print(
                f"    - {auth.get('baseName')} chunks={auth.get('chunks')} "
                f"decoded={auth.get('decoded')} expiresAt={auth.get('expiresAtUtc')} "
                f"jwtExp={auth.get('accessTokenExpUtc')} "
                f"refreshToken={auth.get('refreshTokenPresent')} "
                f"providerToken={auth.get('providerTokenPresent')}"
            )
    console.print("  [dim]browser state after import:[/]")
    if not browser:
        console.print("    unavailable")
    elif browser.get("error"):
        console.print(f"    error={browser['error']}")
    else:
        _field("  url", browser.get("url"))
        _field("  title", browser.get("title"))
        _field("  cloudflare detected", browser.get("cloudflareDetected"))
        _field("  visible cookies", browser.get("cookieNames") or [])
        _field("  visible auth cookies", browser.get("authCookieNames") or [])
        _field("  localStorage keys", browser.get("localStorageKeys") or [])
    if cookie_jar:
        console.print("  [dim]Chrome cookie jar:[/]")
        if cookie_jar.get("error"):
            console.print(f"    error={cookie_jar['error']}")
        else:
            for cookie in cookie_jar.get("cookies") or []:
                flags = [
                    flag
                    for flag, present in (
                        ("secure", cookie.get("secure")),
                        ("httpOnly", cookie.get("httpOnly")),
                    )
                    if present
                ]
                suffix = f" ({', '.join(flags)})" if flags else ""
                console.print(
                    f"    - {cookie.get('name')} domain={cookie.get('domain')} "
                    f"sameSite={cookie.get('sameSite')} expires={cookie.get('expiresUtc')}{suffix}"
                )


# --------------------------------------------------------------------------- #
# lorebook index
# --------------------------------------------------------------------------- #


@main.command("lorebook")
@click.argument("lorebook_id", metavar="LOREBOOK_ID")
@click.option(
    "--limit",
    type=int,
    default=30,
    show_default=True,
    metavar="N",
    help="How many attached characters to print (the saved index always contains all).",
)
@headed_option
def lorebook(lorebook_id: str, limit: int, headed: bool) -> None:
    """Index every public character attached to a JanitorAI lorebook ID.

    The provider returns this relationship from the script endpoint. RIPart
    stores it beside the lorebook so the listed URLs can be extracted later to
    recover and reconcile private entries.
    """
    result = lorebook_task({"lorebook_id": lorebook_id}, **browser_kwargs(headed))
    book = result.get("lorebook") or {}
    characters = result.get("characters") or []
    title = str(book.get("title") or lorebook_id)
    index_path = write_json(
        OUT / "lorebooks" / f"{safe_name(lorebook_id, 'lorebook')}.json", result
    )
    record_paths = update_lorebook_library(
        OUT / "library",
        "",
        {"url": result.get("url") or "", "publicLorebooks": [book]},
    )

    _ok(f"indexed [bold]{title}[/]")
    _field("attached characters", len(characters))
    _path("character index", index_path)
    if record_paths:
        _path("reusable lorebook", record_paths[0])
    if limit > 0 and characters:
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("character")
        table.add_column("creator")
        table.add_column("URL")
        for character in characters[:limit]:
            table.add_row(
                str(character.get("name") or character.get("id") or ""),
                str(character.get("creator") or ""),
                str(character.get("url") or ""),
            )
        console.print(table)
        if len(characters) > limit:
            _field(
                "not shown",
                f"{len(characters) - limit} more (all are in the saved index)",
            )


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #


@main.command()
@click.argument("url", metavar="URL_OR_UUID")
@headed_option
def inspect(url: str, headed: bool) -> None:
    """Fetch a character's public metadata and public lorebooks.

    URL can be a full JanitorAI character URL or just its UUID. This is a quick,
    read-only peek - nothing is triggered on the character.
    """
    result = inspect_task({"url": url}, **browser_kwargs(headed))
    name = safe_name(
        result.get("characterName") or result.get("characterId") or "", "character"
    )
    path = write_json(OUT / "inspections" / f"{name}.json", result)
    _ok(
        f"inspected [bold]{result.get('characterName') or result.get('characterId')}[/]"
    )
    _path("inspection", path)
    _field("public lorebooks", len(result.get("publicLorebooks") or []))
    _field("card public", result.get("cardPublic"))


# --------------------------------------------------------------------------- #
# extract
# --------------------------------------------------------------------------- #


@main.command()
@click.argument("url", metavar="URL_OR_UUID")
@click.option(
    "--delete-chat-on-error",
    is_flag=True,
    default=False,
    help="Delete the temporary chat if extraction fails (default: keep it for retry).",
)
@click.option(
    "--max-trigger-passes",
    type=int,
    default=8,
    show_default=True,
    metavar="N",
    help="Max lorebook trigger messages (card/catalog/greeting chunks).",
)
@click.option(
    "--trigger-chunk-size",
    type=int,
    default=2500,
    show_default=True,
    metavar="CHARS",
    help="Max characters per trigger chunk when splitting long text.",
)
@click.option(
    "--trigger-settle-ms",
    type=int,
    default=0,
    show_default=True,
    metavar="MS",
    help="Optional pause between trigger passes, in milliseconds (usually unneeded).",
)
@click.option(
    "--no-multi-trigger",
    is_flag=True,
    default=False,
    help="Send only one full card trigger (skip the extra keyword passes).",
)
@click.option(
    "--find-triggers",
    is_flag=True,
    default=False,
    help="Probe recovered private lore for likely activation keys (many extra generations).",
)
@click.option(
    "--max-trigger-search-passes",
    type=int,
    default=48,
    show_default=True,
    metavar="N",
    help="[--find-triggers] Cap one-candidate trigger probes.",
)
@click.option(
    "--jllm-leak/--no-jllm-leak",
    default=True,
    show_default=True,
    help="For allow_proxy=false characters, reconstruct the definition with the "
    "multi-pass JanitorLLM fallback (lossy; marked reconstructed-jllm).",
)
@verbose_option
@headed_option
def extract(
    url: str,
    delete_chat_on_error: bool,
    max_trigger_passes: int,
    trigger_chunk_size: int,
    trigger_settle_ms: int,
    no_multi_trigger: bool,
    find_triggers: bool,
    max_trigger_search_passes: int,
    jllm_leak: bool,
    verbose: int,
    headed: bool,
) -> None:
    """Rip a character's private card + lorebook via generateAlpha.

    URL can be a full JanitorAI character URL or just its UUID. Requires an
    active login (see [bold]rip janitor login[/]). Works entirely through direct API
    calls (no chat UI), so it is fast. Stores a single self-contained card PNG
    (V3 card + embedded lorebook) at [cyan]output/cli/library/<uuid>.png[/].

    Paste a [bold]saucepan.ai/companion/<id>[/] URL and it is ripped directly
    through Saucepan's API (no browser); see [bold]rip saucepan[/]. A
    [bold]clank.world/chat/<id>[/] URL is routed to [bold]rip clank[/], and a
    [bold]spicychat.ai/chatbot/<id>[/] URL to [bold]rip spicychat[/].

    Open archives are ripped straight from their public card: a
    [bold]chub.ai/characters/<creator>/<slug>[/] URL, a
    [bold]character-tavern.com/character/<path>[/] URL, or any direct card-file
    URL ([cyan].png[/]/[cyan].charx[/]/[cyan].json[/]) is downloaded and parsed
    with no login.
    """
    if ck.is_clank_url(url):
        _clank_extract(url, verbose=verbose)
        return
    if sp.is_saucepan_url(url):
        _saucepan_extract(url, verbose=verbose)
        return
    if sc.is_spicychat_url(url):
        _spicychat_extract(url, verbose=verbose)
        return
    if tv.is_card_url(url):
        # A direct card file (or character-tavern page) beats the chub check so a
        # chub/CT CDN card URL isn't mistaken for a site page.
        _tavern_extract(url, verbose=verbose)
        return
    if cb.is_chub_url(url):
        _chub_extract(url, verbose=verbose)
        return

    started = time.monotonic()
    result = extract_task(
        {
            "url": url,
            "delete_chat_on_error": delete_chat_on_error,
            "verbose": verbose,
            "max_trigger_passes": 1 if no_multi_trigger else max_trigger_passes,
            "trigger_chunk_size": trigger_chunk_size,
            "trigger_settle_ms": trigger_settle_ms,
            "find_triggers": find_triggers,
            "max_trigger_search_passes": max_trigger_search_passes,
            "jllm_leak": jllm_leak,
        },
        **browser_kwargs(headed),
    )
    elapsed = time.monotonic() - started
    paths = save_to_library(OUT / "library", result.get("characterId") or "", result)
    publish_saved_card(result.get("characterId") or "", result, paths)

    _ok(
        f"extracted [bold]{result.get('characterName') or result.get('characterId') or url}[/]"
    )
    _path("card png", paths["png"])
    _field("entries found", len(result.get("entries") or []))
    if (result.get("character") or {}).get("definitionSource") == "reconstructed-jllm":
        _field("definition", "[yellow]reconstructed via JanitorLLM (lossy)[/]")
    _field("time", _fmt_duration(elapsed))
    if result.get("chatId"):
        _field("chat kept for retry", result["chatId"])

    if verbose:
        _print_extract_diagnostics(result.get("diagnostics") or {})


def _print_extract_diagnostics(diagnostics: dict) -> None:
    console.print("\n[bold]verbose diagnostics[/]")
    if not diagnostics:
        console.print("  unavailable")
        return
    _field("public lorebooks", diagnostics.get("publicLorebookCount", 0))
    _field("public lorebook entries", diagnostics.get("publicEntryCount", 0))
    _field("trigger passes", len(diagnostics.get("triggerPasses") or []))
    _field("merged lorebook entries", diagnostics.get("mergedEntries", 0))
    if "triggerSearchPasses" in diagnostics:
        _field("trigger search probes", diagnostics["triggerSearchPasses"])
        _field("entries with inferred keys", diagnostics.get("triggersFound", 0))
        _field("always-active entries", diagnostics.get("constantEntries", 0))
    for trigger_pass in diagnostics.get("triggerPasses") or []:
        console.print(
            f"    - pass {trigger_pass.get('index')}: {trigger_pass.get('chars', 0)} chars, "
            f"{trigger_pass.get('entriesFound', 0)} entries ({trigger_pass.get('loreChars', 0)} lore chars)"
        )


# --------------------------------------------------------------------------- #
# recent
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--limit",
    type=int,
    default=20,
    show_default=True,
    metavar="N",
    help="How many of the most-recent cards to fetch (pages through the listing).",
)
@click.option(
    "--sfw",
    is_flag=True,
    default=False,
    help="Exclude NSFW cards (default: include everything).",
)
@click.option(
    "--extract",
    "do_extract",
    is_flag=True,
    default=False,
    help="Full-rip each listed card (card + lorebook), not just list them.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="[--extract] Re-rip cards already in the library (default: skip them).",
)
@click.option(
    "--max-trigger-passes",
    type=int,
    default=8,
    show_default=True,
    metavar="N",
    help="[--extract] Max lorebook trigger messages per card.",
)
@click.option(
    "--trigger-chunk-size",
    type=int,
    default=2500,
    show_default=True,
    metavar="CHARS",
    help="[--extract] Max characters per trigger chunk.",
)
@click.option(
    "--no-multi-trigger",
    is_flag=True,
    default=False,
    help="[--extract] Send only one full card trigger per card.",
)
@click.option(
    "--find-triggers",
    is_flag=True,
    default=False,
    help="[--extract] Probe recovered private lore for likely activation keys (many extra generations).",
)
@click.option(
    "--max-trigger-search-passes",
    type=int,
    default=48,
    show_default=True,
    metavar="N",
    help="[--extract --find-triggers] Cap one-candidate trigger probes per card.",
)
@click.option(
    "--delete-chat-on-error",
    is_flag=True,
    default=False,
    help="[--extract] Delete the temporary chat if a card fails.",
)
@click.option(
    "--jllm-leak/--no-jllm-leak",
    default=True,
    show_default=True,
    help="[--extract] Reconstruct allow_proxy=false cards with the multi-pass "
    "JanitorLLM fallback (phase 2; lossy).",
)
@verbose_option
@headed_option
def recent(
    limit: int,
    sfw: bool,
    do_extract: bool,
    force: bool,
    max_trigger_passes: int,
    trigger_chunk_size: int,
    no_multi_trigger: bool,
    find_triggers: bool,
    max_trigger_search_passes: int,
    delete_chat_on_error: bool,
    jllm_leak: bool,
    verbose: int,
    headed: bool,
) -> None:
    """List the most-recent characters (newest first), optionally ripping them.

    By default just prints and saves the listing. Add [bold]--extract[/] to
    full-rip every listed card into the UUID-keyed library as
    [cyan]output/cli/library/<uuid>.png[/] (this creates a temporary chat +
    persona per card and toggles your profile into extraction mode, restoring
    it after). Each successful card is saved immediately, so completed captures
    survive an interrupted batch. The extractor automatically uses metadata when
    complete, the exact proxy capture for proxy-enabled cards (including closed
    lorebooks), and the multi-pass JanitorLLM fallback when proxies are disabled.
    Cards already in the library are skipped unless [bold]--force[/].
    """
    # UUIDs already ripped - the task skips these unless --force.
    library_dir = OUT / "library"
    existing = (
        [path.stem for path in library_dir.glob("*.png")]
        if library_dir.exists()
        else []
    )

    started = time.monotonic()
    result = recent_task(
        {
            "limit": limit,
            "sfw": sfw,
            "extract": do_extract,
            "force": force,
            "existing": existing,
            "checkpoint_library_dir": str(library_dir),
            "verbose": verbose,
            "max_trigger_passes": 1 if no_multi_trigger else max_trigger_passes,
            "trigger_chunk_size": trigger_chunk_size,
            "find_triggers": find_triggers,
            "max_trigger_search_passes": max_trigger_search_passes,
            "delete_chat_on_error": delete_chat_on_error,
            "jllm_leak": jllm_leak,
        },
        **browser_kwargs(headed),
    )
    elapsed = time.monotonic() - started

    cards = result.get("cards") or []
    list_path = write_json(OUT / "recent" / "recent.json", cards)
    _print_recent_table(cards)
    _ok(f"listed [bold]{len(cards)}[/] recent card(s)")
    _path("listing", list_path)

    extracted = result.get("extracted")
    if extracted is None:
        console.print(
            f"\n[dim]run again with [bold]--extract[/] to rip these cards · {_fmt_duration(elapsed)}[/]"
        )
        return

    _write_extracts(extracted)
    console.print(f"\n[dim]total time: {_fmt_duration(elapsed)}[/]")


def _print_recent_table(cards: list) -> None:
    if not cards:
        _no("no cards returned")
        return
    table = Table(box=None, pad_edge=False, show_edge=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("name", style="bold")
    table.add_column("creator", style="cyan")
    table.add_column("nsfw", justify="center")
    table.add_column("card", justify="center")
    table.add_column("uuid", style="dim")
    for index, card in enumerate(cards, start=1):
        table.add_row(
            str(index),
            (card.get("name") or "").strip()[:48] or "-",
            (card.get("creator") or "")[:20],
            "[red]18+[/]" if card.get("nsfw") else "[green]sfw[/]",
            "[green]public[/]" if card.get("cardPublic") else "[yellow]closed[/]",
            card.get("id") or "",
        )
    console.print(table)


def _write_extracts(extracted: list) -> None:
    ok_count = sum(1 for e in extracted if e.get("ok"))
    skipped = sum(1 for e in extracted if e.get("skipped"))
    forbidden = sum(1 for e in extracted if e.get("forbidden"))
    reconstructed = sum(1 for e in extracted if e.get("ok") and e.get("reconstructed"))
    extras = []
    if skipped:
        extras.append(f"{skipped} already in library")
    if forbidden:
        extras.append(f"{forbidden} proxy-disabled")
    if reconstructed:
        extras.append(f"{reconstructed} JanitorLLM-reconstructed")
    summary = f"\n[bold]extracted {ok_count}/{len(extracted)} card(s)[/]"
    if extras:
        summary += f" [dim]({', '.join(extras)})[/]"
    console.print(summary)
    for entry in extracted:
        if entry.get("skipped"):
            console.print(
                f"[dim]↷ {entry.get('name')} - already extracted (use --force)[/]"
            )
            continue
        if entry.get("forbidden"):
            console.print(
                f"[dim]⊘ {entry.get('name')} - proxies disabled (pass --jllm-leak to reconstruct)[/]"
            )
            continue
        if not entry.get("ok"):
            _no(f"{entry.get('name')} - {entry.get('error')}")
            continue
        result = entry.get("result") or {}
        paths = entry.get("saved_paths") or save_to_library(
            OUT / "library", result.get("characterId") or "", result
        )
        secs = entry.get("seconds")
        timing = f" [dim]({secs}s)[/]" if secs is not None else ""
        tag = " [yellow](jllm-reconstructed)[/]" if entry.get("reconstructed") else ""
        _ok(
            f"{result.get('characterName') or entry.get('name')}{tag} - {entry.get('entries', 0)} entries{timing} → [cyan]{paths['png']}[/]"
        )


def _fmt_expiry(seconds: float) -> str:
    """Coarse human duration for token lifetimes (days / hours / minutes)."""
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{max(0, int(seconds // 60))}m"


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m{secs:02d}s"


# --------------------------------------------------------------------------- #
# janitor
# --------------------------------------------------------------------------- #


@main.group(cls=click.RichGroup)
def janitor() -> None:
    """Rip characters from [bold]JanitorAI[/] through its browser-backed API.

    Typical flow:

    \b
      1. rip janitor login
      2. rip janitor status
      3. rip janitor list
      4. rip janitor extract <url>

    """


# Register each JanitorAI command once under its provider group.
for _janitor_command in (status, login, import_session, lorebook, inspect, extract, recent):
    janitor.add_command(_janitor_command)
janitor.add_command(recent, "list")

# ``extract`` remains at the root as the supported cross-provider URL router.
# All other JanitorAI commands must be invoked through ``rip janitor``.
for _janitor_root_command in ("status", "login", "import-session", "lorebook", "inspect", "recent"):
    main.commands.pop(_janitor_root_command, None)


# --------------------------------------------------------------------------- #
# status (cross-provider overview)
# --------------------------------------------------------------------------- #


@main.command("status")
def status_overview() -> None:
    """Show the auth state of every provider plus the local library."""
    console.print("[bold]Providers[/]")

    # JanitorAI: a real login check needs the browser, so this only reports a
    # saved session. ponytail: cheap file probe; `rip janitor status` verifies.
    if state_path("janitor-session.json").exists():
        _ok("janitor    session saved [dim](verify with [bold]rip janitor status[/])[/]")
    else:
        _no("janitor    no session [dim](run [bold]rip janitor login[/])[/]")

    if sp.has_token():
        exp = sp.token_expiry()
        remaining = None if exp is None else exp - time.time()
        if remaining is not None and remaining <= 0:
            _no("saucepan   token expired [dim](run [bold]rip saucepan login[/])[/]")
        elif remaining is not None:
            _ok(f"saucepan   token configured [dim](expires in {_fmt_expiry(remaining)})[/]")
        else:
            _ok("saucepan   token configured")
    else:
        _no("saucepan   no token [dim](run [bold]rip saucepan login[/])[/]")

    if ck.has_session():
        has_csrf = bool(ck.load_session().get("csrf_token"))
        _ok("clank      session configured" + ("" if has_csrf else " [dim](no csrf token)[/]"))
    else:
        _no("clank      no session [dim](run [bold]rip clank login[/])[/]")

    if sc.has_token():
        remaining = sc.token_expiry() - time.time()
        if remaining <= 0:
            _no("spicychat  token expired [dim](run [bold]rip spicychat login[/])[/]")
        else:
            _ok(f"spicychat  session configured [dim](expires in {_fmt_expiry(remaining)})[/]")
    else:
        _no("spicychat  guest only [dim](run [bold]rip spicychat login[/] for gated cards)[/]")

    library = OUT / "library"
    cards = len(list(library.glob("*.png"))) if library.is_dir() else 0
    console.print("\n[bold]Library[/]")
    _field("cards", cards)
    _path("location", library)


# --------------------------------------------------------------------------- #
# saucepan
# --------------------------------------------------------------------------- #


@main.group(cls=click.RichGroup)
def saucepan() -> None:
    """Rip companions from [bold]saucepan.ai[/] via its REST API (no browser).

    Saucepan serves companion definitions through an authenticated API, so
    ripping is a direct, exact pull rather than a browser reconstruction. Log in
    once, then extract:

    \b
      1. rip saucepan login            store a bearer token (reused afterwards)
      2. rip saucepan status           confirm a token is configured
      3. rip saucepan list             browse newest companions
      4. rip saucepan extract <url>    rip a companion card

    A [cyan]saucepan.ai[/] URL passed to [bold]rip extract[/] is routed here too.
    """


@saucepan.command("login")
@click.option(
    "--username", prompt=True, help="Your Saucepan username (prompted if omitted)."
)
@click.option(
    "--password",
    prompt=True,
    hide_input=True,
    help="Your Saucepan password (prompted, hidden, if omitted).",
)
def saucepan_login(username: str, password: str) -> None:
    """Log in with your username + password and store a bearer token."""
    sp.login(username, password)
    _ok("logged in - token saved")
    _path("token file", sp.TOKEN_FILE)


@saucepan.command("status")
def saucepan_status() -> None:
    """Report whether a Saucepan token is configured (and still valid)."""
    if not sp.has_token():
        _no("no Saucepan token - run [bold]rip saucepan login[/] first")
        sys.exit(1)
    exp = sp.token_expiry()
    if exp is not None:
        remaining = exp - time.time()
        if remaining <= 0:
            _no("Saucepan token expired - run [bold]rip saucepan login[/] again")
            sys.exit(1)
        _ok(f"Saucepan token configured [dim](expires in {_fmt_expiry(remaining)})[/]")
    else:
        _ok("Saucepan token configured")
    sys.exit(0)


@saucepan.command("logout")
def saucepan_logout() -> None:
    """Forget the stored Saucepan token."""
    sp.clear_token()
    _ok("Saucepan token cleared")


@saucepan.command("list")
@click.option(
    "--limit",
    type=click.IntRange(min=1, max=96),
    default=30,
    show_default=True,
    help="Max companions to display.",
)
@click.option(
    "--offset", type=click.IntRange(min=0), default=0, show_default=True, help="Results to skip."
)
@click.option("--tag", "tags", multiple=True, metavar="TAG", help="Require a tag (repeatable).")
@click.option(
    "--exclude-tag",
    "excluded_tags",
    multiple=True,
    metavar="TAG",
    help="Exclude a tag (repeatable).",
)
@click.option("--nsfw/--no-nsfw", default=True, show_default=True, help="Include NSFW companions.")
@click.option(
    "--extract",
    "do_extract",
    is_flag=True,
    default=False,
    help="Rip each listed companion into the library; existing cards are skipped.",
)
def saucepan_list(
    limit: int,
    offset: int,
    tags: tuple[str, ...],
    excluded_tags: tuple[str, ...],
    nsfw: bool,
    do_extract: bool,
) -> None:
    """Browse newest Saucepan companions with URLs usable by [bold]extract[/]."""
    try:
        result = sp.search_companions(
            limit=limit,
            offset=offset,
            tags=list(tags),
            excluded_tags=list(excluded_tags),
            include_nsfw=nsfw,
        )
    except sp.SaucepanError as exc:
        _no(str(exc))
        if exc.status == 401:
            err_console.print("[dim]run [bold]rip saucepan login[/] to authenticate[/]")
        raise SystemExit(1)

    companions = result["companions"]
    if not companions:
        _no("no companions returned")
        return

    table = Table(box=None, pad_edge=False, show_edge=False)
    table.add_column("#", style="dim", justify="right")
    table.add_column("companion", style="bold")
    table.add_column("creator", style="cyan")
    table.add_column("posted", style="cyan")
    table.add_column("chats", justify="right")
    table.add_column("tags", style="dim")
    table.add_column("url")
    for index, companion in enumerate(companions, offset + 1):
        companion_id = str(companion.get("id") or companion.get("companion_id") or "")
        name = str(companion.get("display_name") or companion.get("name") or "?")
        tags = companion.get("tags") if isinstance(companion.get("tags"), list) else []
        nsfw_mark = " [red]nsfw[/]" if companion.get("is_nsfw") or companion.get("sus") else ""
        table.add_row(
            str(index),
            name[:34] + nsfw_mark,
            str(companion.get("author_handle") or "?"),
            str(companion.get("posted_at") or "")[:10],
            str(companion.get("chat_count") or "0"),
            ", ".join(str(tag) for tag in tags[:4]),
            f"{sp.SAUCEPAN_ORIGIN}/companion/{companion_id}" if companion_id else "-",
        )
    console.print(table)
    total_count = result.get("total_count")
    total = str(total_count) if isinstance(total_count, int) else "?"
    _field("listed", f"{len(companions)} of {total} companion(s) (offset={offset})")

    if do_extract:
        cli_extractors.save_listed_cards(
            _extraction_ui(), companions,
            item_id=lambda item: str(item.get("id") or item.get("companion_id") or ""),
            item_name=lambda item: str(item.get("display_name") or item.get("name") or "?"),
            extract=lambda item: sp.extract_companion(str(item.get("id") or item.get("companion_id") or "")),
        )


@saucepan.command("providers")
def saucepan_providers() -> None:
    """List your BYOK model provider configs (usable with [bold]extract --leak[/])."""
    configs = sp.list_provider_configs()
    if not configs:
        _no("no provider configs - add one on saucepan.ai (Settings → Model Providers)")
        return
    table = Table(box=None, pad_edge=False, show_edge=False)
    table.add_column("name", style="bold")
    table.add_column("model", style="cyan")
    table.add_column("provider")
    table.add_column("config_id", style="dim")
    for cfg in configs:
        table.add_row(
            str(cfg.get("config_name") or "-"),
            str(cfg.get("model_id") or "-"),
            str(cfg.get("provider") or "-"),
            str(cfg.get("config_id") or ""),
        )
    console.print(table)


@saucepan.command("extract")
@click.argument("url", metavar="URL_OR_UUID")
@click.option(
    "--no-lorebooks",
    is_flag=True,
    default=False,
    help="Skip the companion's attached lorebooks (card only).",
)
@click.option(
    "--leak",
    is_flag=True,
    default=False,
    help="Recover the gated definition (example dialogue / advanced prompt) by having a "
    "model dump it in a throwaway chat. Creates a chat and spends a generation. Lossy; "
    "marks the card 'saucepan-leak'.",
)
@click.option(
    "--leak-config",
    metavar="NAME_OR_ID",
    default=None,
    help="[--leak] BYOK provider config to run the leak through (name or id; see "
    "[bold]rip saucepan providers[/]). Saucepan's default model refuses, so a compliant "
    "model is needed. Defaults to your first visible config.",
)
@click.option(
    "--leak-model",
    metavar="ALIAS",
    default=None,
    help="[--leak] Use a Saucepan model_alias instead of a BYOK provider config.",
)
@click.option(
    "--leak-mode",
    type=click.Choice(["user", "director"]),
    default="user",
    show_default=True,
    help="[--leak] Generation mode. 'user' works with the widest range of models; "
    "some models return nothing in 'director' mode.",
)
@click.option(
    "--leak-prompt",
    metavar="TEXT",
    default=None,
    help="[--leak] Override the message sent to the model. Keep it short and generic — "
    "Saucepan blocks prompts that name the protected sections or use jailbreak phrasing.",
)
@click.option(
    "--leak-system",
    metavar="TEXT",
    default=None,
    help="[--leak, BYOK only] Temporarily set the provider config's system prompt "
    "(\"Provider Pre Content Prompt\") for the leak, then restore it. Helps a capable "
    "model dump verbatim; needs --leak-config (not --leak-model).",
)
@click.option(
    "--leak-keep",
    is_flag=True,
    default=False,
    help="[--leak] Accept the model's reply even if it doesn't look like a definition "
    "dump (by default such replies are retried, since models often just keep roleplaying).",
)
@click.option(
    "--leak-echo/--no-leak-echo",
    default=True,
    help="[--leak] Prefer the verbatim echo-proxy leak (points a BYOK --leak-config's "
    "provider_url at an echo endpoint to reflect the assembled prompt back). Falls back "
    "to the model dump if the account doesn't allow custom endpoints. On by default.",
)
@verbose_option
def saucepan_extract(
    url: str,
    no_lorebooks: bool,
    leak: bool,
    leak_config: str | None,
    leak_model: str | None,
    leak_mode: str,
    leak_prompt: str | None,
    leak_system: str | None,
    leak_keep: bool,
    leak_echo: bool,
    verbose: int,
) -> None:
    """Rip a Saucepan companion card + lorebooks by URL (or bare companion id)."""
    _saucepan_extract(
        url,
        include_lorebooks=not no_lorebooks,
        leak=leak,
        leak_config=leak_config,
        leak_model=leak_model,
        leak_mode=leak_mode,
        leak_prompt=leak_prompt,
        leak_system=leak_system,
        leak_keep=leak_keep,
        leak_echo=leak_echo,
        verbose=verbose,
    )




# --------------------------------------------------------------------------- #
# clank
# --------------------------------------------------------------------------- #


@main.group(cls=click.RichGroup)
def clank() -> None:
    """Rip characters from [bold]clank.world[/] via its API (no browser).

    clank.world gates the real character definition, but the full system prompt
    is recoverable verbatim with an [bold]echo proxy[/] — an OpenAI-compatible
    endpoint that echoes clank's request body back. Point a chat's custom LLM
    provider at the proxy, send a message, and the echoed system prompt is the
    definition. Then:

    \b
      1. rip clank login             store your session cookie (reused afterwards)
      2. rip clank status            confirm a session is configured
      3. rip clank extract <url>     rip the character card from a chat

    A [cyan]clank.world/chat/<id>[/] URL passed to [bold]rip extract[/] is routed
    here too.
    """


@clank.command("login")
@click.option(
    "--session-token",
    prompt="clank.world session token (__Secure-next-auth.session-token)",
    help="The value of the __Secure-next-auth.session-token cookie from your browser.",
)
@click.option(
    "--csrf-token",
    default=None,
    help="The __Host-next-auth.csrf-token cookie value (needed only for --leak auto-generation).",
)
def clank_login(session_token: str, csrf_token: str | None) -> None:
    """Store your clank.world session cookie (copied from your browser).

    clank uses next-auth, so there is no username/password API login. Copy the
    [cyan]__Secure-next-auth.session-token[/] cookie from your browser's dev
    tools (Application → Cookies → www.clank.world) and paste it here.
    """
    ck.set_session(session_token, csrf_token)
    _ok("session saved")
    _path("session file", ck.SESSION_FILE)


@clank.command("status")
def clank_status() -> None:
    """Report whether a clank.world session is configured."""
    if not ck.has_session():
        _no("no clank.world session - run [bold]rip clank login[/] first")
        sys.exit(1)
    has_csrf = bool(ck.load_session().get("csrf_token"))
    _ok(
        "clank.world session configured"
        + ("" if has_csrf else " [dim](no csrf token; --leak auto-generation unavailable)[/]")
    )
    sys.exit(0)


@clank.command("logout")
def clank_logout() -> None:
    """Forget the stored clank.world session."""
    ck.clear_session()
    _ok("clank.world session cleared")


@clank.command("list")
@click.option(
    "--sort",
    type=click.Choice(["new", "trending"]),
    default="new",
    show_default=True,
    help="'new' = newest-first; 'trending' = clank's ranked feed.",
)
@click.option("--limit", type=int, default=30, show_default=True, help="Max stories to list.")
@click.option(
    "--tag",
    "tags",
    multiple=True,
    metavar="TAG",
    help="Filter by tag (repeatable). Tags are case-sensitive (e.g. Female, Husband).",
)
@click.option("--nsfw/--no-nsfw", default=True, show_default=True, help="Include NSFW stories.")
@click.option("--page-size", type=int, default=20, show_default=True, help="Items fetched per API page.")
@click.option(
    "--extract",
    "do_extract",
    is_flag=True,
    default=False,
    help="Save a partial card for each listed story (public data only — name, "
    "scenario, greetings, tags, avatar; the definition stays gated). Existing cards are skipped. "
    "Marked clank-partial.",
)
def clank_list(
    sort: str, limit: int, tags: tuple[str, ...], nsfw: bool, page_size: int, do_extract: bool
) -> None:
    """List clank.world stories/characters (newest-first by default).

    Pages the public browse feed and prints the character, tags, chat count, and
    a chat/character URL you can pass to [bold]rip clank extract[/]. With
    [bold]--extract[/] each is also saved as a partial card (public data only).
    """
    if not ck.has_session():
        _no("no clank.world session - run [bold]rip clank login[/] first")
        sys.exit(1)
    try:
        items = list(
            ck.iter_stories(
                sort=sort,
                limit=limit,
                page_size=page_size,
                tags=list(tags) or None,
                include_nsfw=nsfw,
            )
        )
    except ck.ClankError as exc:
        _no(str(exc))
        sys.exit(1)

    if not items:
        _no("no stories returned")
        return

    table = Table(box=None, pad_edge=False, show_edge=False)
    table.add_column("#", style="dim", justify="right")
    table.add_column("character", style="bold")
    table.add_column("created", style="cyan")
    table.add_column("chats", justify="right")
    table.add_column("tags", style="dim")
    table.add_column("character url")
    for i, it in enumerate(items, 1):
        created = str(it.get("created_at") or "")[:10]
        tag_list = it.get("tags") if isinstance(it.get("tags"), list) else []
        nsfw_mark = " [red]nsfw[/]" if it.get("is_nsfw") else ""
        table.add_row(
            str(i),
            (str(it.get("agent_name") or "?")[:34]) + nsfw_mark,
            created,
            str(it.get("total_chats") or "0"),
            ", ".join(tag_list[:4]),
            ck.story_character_url(it),
        )
    console.print(table)
    _field("listed", f"{len(items)} stories (sort={sort})")

    if do_extract:
        cli_extractors.save_listed_cards(
            _extraction_ui(), items,
            item_id=lambda item: str(item.get("agent_id") or item.get("id") or ""),
            item_name=lambda item: str(item.get("agent_name") or "?"),
            extract=ck.extract_story,
            source_url=ck.story_character_url,
            suffix=" [dim](clank-partial)[/]",
        )


@clank.command("extract")
@click.argument("url", metavar="URL_OR_UUID")
@click.option(
    "--leak",
    is_flag=True,
    default=False,
    help="If the chat has no echoed reply yet, auto-configure the echo proxy and "
    "send a message to force one, then restore the original provider. Needs the "
    "csrf token (see [bold]rip clank login[/]).",
)
@click.option(
    "--keep-boilerplate",
    is_flag=True,
    default=False,
    help="Keep the generic clank RP/formatting instructions in the card's creator notes.",
)
@click.option(
    "--trigger-message",
    metavar="TEXT",
    default="hi",
    show_default=True,
    help="[--leak] The throwaway message sent to trigger a generation.",
)
@click.option(
    "--lorebook",
    is_flag=True,
    default=False,
    help="After leaking the definition, fire the character's lorebook by sending "
    "trigger messages built from its own text (description/scenario/greeting) and "
    "diffing the expanded echoes. Best on a FRESH chat (memory accumulates otherwise).",
)
@click.option(
    "--max-triggers",
    type=int,
    default=8,
    show_default=True,
    metavar="N",
    help="[--lorebook] Cap the number of trigger messages sent (each is a slow generation).",
)
@verbose_option
def clank_extract(
    url: str,
    leak: bool,
    keep_boilerplate: bool,
    trigger_message: str,
    lorebook: bool,
    max_triggers: int,
    verbose: int,
) -> None:
    """Rip a clank.world character card from a chat or character URL.

    URL can be a [cyan]clank.world/chat/<uuid>[/] chat URL, a bare chat UUID, or
    a [cyan]clank.world/@<slug>[/] character page — the latter is resolved to your
    existing chat with that character (open one and send a message first).
    """
    _clank_extract(
        url,
        leak=leak,
        keep_boilerplate=keep_boilerplate,
        trigger_message=trigger_message,
        with_lorebook=lorebook,
        max_triggers=max_triggers,
        verbose=verbose,
    )




# --------------------------------------------------------------------------- #
# spicychat
# --------------------------------------------------------------------------- #


@main.group(cls=click.RichGroup)
def spicychat() -> None:
    """Rip characters from [bold]spicychat.ai[/] via its API (no browser).

    spicychat serves a character's definition directly when the creator left it
    public — no login needed, a self-generated guest id is enough:

    \b
      1. rip spicychat search <query>    browse the public catalogue
      2. rip spicychat extract <url>     rip the character card

    When a definition is gated ([cyan]definition_visible=false[/]) only the
    public surface (greeting, tags, avatar) is recovered — a [cyan]spicychat-partial[/]
    card. Logging in ([bold]rip spicychat login[/]) adds NSFW visibility and
    higher rate limits but does [bold]not[/] un-gate a definition. A
    [cyan]spicychat.ai/chatbot/<id>[/] URL passed to [bold]rip extract[/] is
    routed here too.
    """


@spicychat.command("login")
@click.option(
    "--refresh-token",
    prompt="spicychat.ai Kinde refresh token",
    help="The OAuth refresh token from your browser session (auth.spicychat.ai).",
)
def spicychat_login(refresh_token: str) -> None:
    """Store a spicychat.ai refresh token and verify it mints an access token.

    spicychat uses Kinde OAuth, so there is no username/password API login. Copy
    the refresh token from your browser (dev tools → Application → Local/Session
    storage or the token request on [cyan]auth.spicychat.ai[/]) and paste it
    here; the client mints short-lived access tokens from it and rotates it
    automatically.
    """
    sc.set_refresh_token(refresh_token)
    try:
        sc.authenticate()
    except sc.SpicyChatError as exc:
        _no(str(exc))
        sys.exit(1)
    _ok("spicychat.ai session saved")
    _path("session file", sc.SESSION_FILE)


@spicychat.command("status")
def spicychat_status() -> None:
    """Report whether a spicychat.ai login is configured (guest works regardless)."""
    if not sc.has_token():
        _no("no spicychat.ai login [dim](guest extraction of public definitions still works)[/]")
        sys.exit(1)
    try:
        sc.authenticate()
    except sc.SpicyChatError as exc:
        _no(str(exc))
        sys.exit(1)
    _ok("spicychat.ai session configured")
    _field("access token expires in", _fmt_expiry(sc.token_expiry() - time.time()))
    sys.exit(0)


@spicychat.command("logout")
def spicychat_logout() -> None:
    """Forget the stored spicychat.ai session (guest id included)."""
    sc.clear_session()
    _ok("spicychat.ai session cleared")


def _tag_option(func):
    return click.option(
        "--tag",
        "tags",
        multiple=True,
        metavar="TAG",
        help="Filter by tag (repeatable, case-sensitive — e.g. Female, Anime).",
    )(func)


def _catalogue_options(func):
    """The options shared by [rip spicychat search] and [rip spicychat list]."""
    func = click.option(
        "--limit", type=int, default=30, show_default=True, help="Max results to list."
    )(func)
    func = _tag_option(func)
    func = click.option(
        "--nsfw/--no-nsfw", default=True, show_default=True, help="Include NSFW characters."
    )(func)
    func = click.option(
        "--extract",
        "do_extract",
        is_flag=True,
        default=False,
        help="Also rip each listed character into the library (full card when the "
        "definition is public, else a spicychat-partial card). Existing cards are skipped.",
    )(func)
    return verbose_option(func)


@spicychat.command("search")
@click.argument("query", metavar="QUERY")
@_catalogue_options
def spicychat_search(
    query: str,
    limit: int,
    tags: tuple[str, ...],
    nsfw: bool,
    do_extract: bool,
    verbose: int,
) -> None:
    """Text-search the public spicychat.ai catalogue by name/title/tags/creator.

    Prints the character, whether its definition is public, tags and a URL you
    can pass to [bold]rip spicychat extract[/]. With [bold]--extract[/] each is
    also ripped into the library. Use [bold]rip spicychat list[/] to browse
    without a query.
    """
    _spicychat_list(query, limit=limit, tags=tags, nsfw=nsfw, do_extract=do_extract, verbose=verbose)


@spicychat.command("list")
@_catalogue_options
def spicychat_list(
    limit: int,
    tags: tuple[str, ...],
    nsfw: bool,
    do_extract: bool,
    verbose: int,
) -> None:
    """Browse the public spicychat.ai catalogue (most active first).

    Like [bold]rip spicychat search[/] but with no query — pages the catalogue
    ranked by recent activity. Narrow it with [bold]--tag[/] / [bold]--no-nsfw[/],
    and rip each listed character with [bold]--extract[/].
    """
    _spicychat_list("", limit=limit, tags=tags, nsfw=nsfw, do_extract=do_extract, verbose=verbose)


def _spicychat_list(
    query: str,
    *,
    limit: int,
    tags: tuple[str, ...],
    nsfw: bool,
    do_extract: bool,
    verbose: int,
) -> None:
    """Shared impl for [rip spicychat search] and [rip spicychat list]."""
    sc.set_trace_level(verbose)
    try:
        result = sc.search_characters(
            query, limit=limit, tags=list(tags) or None, include_nsfw=nsfw
        )
    except sc.SpicyChatError as exc:
        _no(str(exc))
        sys.exit(1)
    finally:
        sc.set_trace_level(0)

    hits = result.get("hits") or []
    if not hits:
        _no("no characters returned")
        return

    table = Table(box=None, pad_edge=False, show_edge=False)
    table.add_column("#", style="dim", justify="right")
    table.add_column("character", style="bold")
    table.add_column("def", justify="center")
    table.add_column("chats", justify="right")
    table.add_column("tags", style="dim")
    table.add_column("url")
    for i, doc in enumerate(hits, 1):
        tag_list = doc.get("tags") if isinstance(doc.get("tags"), list) else []
        nsfw_mark = " [red]nsfw[/]" if doc.get("is_nsfw") else ""
        public = "[green]public[/]" if doc.get("definition_visible") else "[yellow]gated[/]"
        table.add_row(
            str(i),
            (str(doc.get("name") or "?")[:34]) + nsfw_mark,
            public,
            str(doc.get("num_messages") or "0"),
            ", ".join(tag_list[:4]),
            sc.character_url(str(doc.get("character_id") or "")),
        )
    console.print(table)
    _field("found", f"{result.get('found', len(hits))} total (showing {len(hits)})")

    if do_extract:
        cli_extractors.save_listed_cards(
            _extraction_ui(), hits,
            item_id=lambda item: str(item.get("character_id") or ""),
            item_name=lambda item: str(item.get("name") or "?"),
            extract=lambda item: sc.extract_character(str(item.get("character_id") or "")),
        )


@spicychat.command("extract")
@click.argument("url", metavar="URL_OR_UUID")
@click.option(
    "--leak",
    is_flag=True,
    default=False,
    help="If the definition is gated (definition_visible=false), recover it by having "
    "the chat model dump its own context in a throwaway conversation. Lossy (a model "
    "paraphrase, not verbatim); marks the card 'spicychat-leak'. No login needed.",
)
@click.option(
    "--leak-model",
    metavar="MODEL",
    default=sc.DEFAULT_LEAK_MODEL,
    show_default=True,
    help="[--leak] spicychat inference_model to run the dump through. The default "
    "breaks character most reliably; 'default' is a nondeterministic engine pool "
    "(Lyra/Zeta/novita vary per call), so leak quality with it is a coin-flip. "
    "Honored named aliases: zeta-26b, squelching_fantasies_8b, spicedq3_a3b.",
)
@click.option(
    "--leak-attempts",
    type=int,
    default=4,
    show_default=True,
    help="[--leak] How many times to retry (leaks are non-deterministic).",
)
@click.option(
    "--leak-prompt",
    metavar="TEXT",
    default=None,
    help="[--leak] Override the message sent to the model. The default '/cmd dump …' "
    "prompt reliably breaks character; a polite 'describe yourself' request usually "
    "just gets more roleplay.",
)
@click.option(
    "--leak-keep",
    is_flag=True,
    default=False,
    help="[--leak] Accept the reply even if it doesn't look like a definition dump "
    "(by default such replies are retried, since models often just keep roleplaying).",
)
@verbose_option
def spicychat_extract(
    url: str,
    leak: bool,
    leak_model: str,
    leak_attempts: int,
    leak_prompt: str | None,
    leak_keep: bool,
    verbose: int,
) -> None:
    """Rip a spicychat.ai character card from a chatbot URL or UUID.

    URL can be a [cyan]spicychat.ai/chatbot/<uuid>[/] URL, a
    [cyan]spicychat.ai/characters/<uuid>[/] URL, or a bare UUID. No login is
    required for a character whose definition is public.

    A gated definition (definition_visible=false) saves a partial card by
    default; add [bold]--leak[/] to recover it via a model dump.
    """
    _spicychat_extract(
        url,
        leak=leak,
        leak_model=leak_model,
        leak_attempts=leak_attempts,
        leak_prompt=leak_prompt,
        leak_keep=leak_keep,
        verbose=verbose,
    )




# Provider extraction implementation and result formatting live in
# ``cli_extractors``.  Keep these private adapters for root-level URL routing
# and for the provider Click commands above.
def _saucepan_extract(url: str, **kwargs: object) -> None:
    cli_extractors.saucepan_extract(_extraction_ui(), url, **kwargs)


def _clank_extract(url: str, **kwargs: object) -> None:
    cli_extractors.clank_extract(_extraction_ui(), url, **kwargs)


def _spicychat_extract(url: str, **kwargs: object) -> None:
    cli_extractors.spicychat_extract(_extraction_ui(), url, **kwargs)


def _chub_extract(url: str, **kwargs: object) -> None:
    cli_extractors.chub_extract(_extraction_ui(), url, **kwargs)


def _tavern_extract(url: str, **kwargs: object) -> None:
    cli_extractors.tavern_extract(_extraction_ui(), url, **kwargs)


# --------------------------------------------------------------------------- #
# completion
# --------------------------------------------------------------------------- #

_COMPLETION_SNIPPETS = {
    "bash": 'eval "$(_RIP_COMPLETE=bash_source rip)"    # add to ~/.bashrc',
    "zsh": 'eval "$(_RIP_COMPLETE=zsh_source rip)"     # add to ~/.zshrc',
    "fish": "_RIP_COMPLETE=fish_source rip | source     # add to ~/.config/fish/config.fish",
}


@main.command()
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]), required=False)
def completion(shell: str | None) -> None:
    """Show how to enable tab-completion for your shell.

    With no argument, prints the snippet for every supported shell. Add the line
    for your shell to its startup file, then restart the shell.
    """
    shells = [shell] if shell else list(_COMPLETION_SNIPPETS)
    console.print("[bold]Enable tab-completion for `rip`[/]\n")
    for name in shells:
        console.print(f"[bold cyan]{name}[/]")
        console.print(f"  {_COMPLETION_SNIPPETS[name]}\n")


@main.command("discord-bot")
@discord_verbose_option
@click.option(
    "--reload",
    "reload_",
    is_flag=True,
    help="Dev: hot-patch code changes into the live bot without dropping running jobs.",
)
def discord_bot(verbose: int, reload_: bool) -> None:
    """Serve the one-at-a-time `/rip` Discord command gateway."""
    from .common.discord_bot import run_discord_bot

    if reload_:
        try:
            import jurigged
        except ImportError:
            raise SystemExit("--reload needs jurigged — install it with `uv sync --extra discord`")
        # Hot-patch changed functions straight into the running interpreter
        # instead of restarting, so extractions already in flight keep going.
        # Caveat: structural changes (new/removed slash commands, changed command
        # schemas, class layout) still need a manual restart — the command tree
        # is built and synced once at startup and jurigged only patches function
        # bodies.
        jurigged.watch(str(Path(__file__).resolve().parent))
        console.print(
            "[dim]hot-reload on (jurigged) — saved edits patch the live bot; "
            "restart for new/renamed commands[/]"
        )

    run_discord_bot(verbose=verbose)


if __name__ == "__main__":
    main()
