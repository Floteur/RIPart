"""Command-line interface for RIPart.

Thin, user-facing layer: it parses arguments, calls the Botasaurus tasks in
``browser_tasks``, and prints friendly results. All the real work happens in
``browser_tasks`` and ``helpers``.

Run ``rip --help`` (or ``python -m ripart --help``) to get started.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import rich_click as click
from rich.console import Console
from rich.table import Table

from .common.cards import save_to_library
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
    recent_task,
    status_task,
)

# --------------------------------------------------------------------------- #
# Paths & shared console
# --------------------------------------------------------------------------- #

# Keep everything self-contained under this project directory.
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output" / "cli"

console = Console()
err_console = Console(stderr=True)


# --------------------------------------------------------------------------- #
# Beautiful help configuration (rich-click)
# --------------------------------------------------------------------------- #

click.rich_click.USE_RICH_MARKUP = True
click.rich_click.USE_MARKDOWN = False
click.rich_click.SHOW_ARGUMENTS = True
click.rich_click.GROUP_ARGUMENTS_OPTIONS = False
click.rich_click.SHOW_METAVARS_COLUMN = True
click.rich_click.APPEND_METAVARS_HELP = False
click.rich_click.STYLE_OPTION = "bold cyan"
click.rich_click.STYLE_ARGUMENT = "bold cyan"
click.rich_click.STYLE_COMMAND = "bold green"
click.rich_click.STYLE_OPTIONS_TABLE_BOX = "SIMPLE"
click.rich_click.STYLE_COMMANDS_TABLE_BOX = "SIMPLE"
click.rich_click.MAX_WIDTH = 100

# Group the sub-commands into tidy, labelled panels in `rip --help`.
click.rich_click.COMMAND_GROUPS = {
    "rip": [
        {"name": "Session & login", "commands": ["status", "login", "import-session"]},
        {"name": "Ripping", "commands": ["inspect", "extract", "recent"]},
        {"name": "Saucepan", "commands": ["saucepan"]},
        {"name": "Clank", "commands": ["clank"]},
        {"name": "Spicychat", "commands": ["spicychat"]},
        {"name": "Setup", "commands": ["completion"]},
    ],
    "rip saucepan": [
        {"name": "Session & login", "commands": ["login", "status", "logout"]},
        {"name": "Ripping", "commands": ["extract", "providers"]},
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
      1. rip login                     log in once (reused afterwards)
      2. rip status                    confirm you are logged in
      3. rip inspect <url>             peek at a character's public metadata
      4. rip extract <url>             rip the full card + lorebook

    [bold]rip extract <url>[/] also accepts Saucepan and clank.world URLs; see
    [bold]rip saucepan[/] and [bold]rip clank[/] for their own login/extract
    commands. Results are written under [cyan]output/cli/[/]. Run
    [bold]rip COMMAND --help[/] for details on any command.
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
    _no("not logged in - run [bold]rip login[/] first")
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
    "--jllm-leak",
    is_flag=True,
    default=False,
    help="For allow_proxy=false characters: reconstruct the definition via a "
    "JanitorLLM injection leak (lossy; marked reconstructed-jllm).",
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
    jllm_leak: bool,
    verbose: int,
    headed: bool,
) -> None:
    """Rip a character's private card + lorebook via generateAlpha.

    URL can be a full JanitorAI character URL or just its UUID. Requires an
    active login (see [bold]rip login[/]). Works entirely through direct API
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
            "jllm_leak": jllm_leak,
        },
        **browser_kwargs(headed),
    )
    elapsed = time.monotonic() - started
    paths = save_to_library(OUT / "library", result.get("characterId") or "", result)

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
    "--delete-chat-on-error",
    is_flag=True,
    default=False,
    help="[--extract] Delete the temporary chat if a card fails.",
)
@click.option(
    "--jllm-leak",
    is_flag=True,
    default=False,
    help="[--extract] Also rip allow_proxy=false cards by reconstructing their "
    "definition via a JanitorLLM injection leak (phase 2; lossy).",
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
    it after). Cards already in the library are skipped unless [bold]--force[/].
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
            "verbose": verbose,
            "max_trigger_passes": 1 if no_multi_trigger else max_trigger_passes,
            "trigger_chunk_size": trigger_chunk_size,
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
        paths = save_to_library(
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
      3. rip saucepan extract <url>    rip a companion card

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


def _rank_leak_configs(configs: list[dict]) -> list[dict]:
    """Order provider configs for --leak auto-pick: visible first, real providers
    before ad-hoc 'custom' ones (which are often half-configured), then by the
    user's own sort_order."""
    visible = [c for c in configs if c.get("is_visible")]

    def rank(cfg: dict) -> tuple:
        is_custom = 1 if str(cfg.get("provider") or "").lower() == "custom" else 0
        return (is_custom, cfg.get("sort_order") if isinstance(cfg.get("sort_order"), int) else 999)

    return sorted(visible, key=rank)


def _saucepan_extract(
    url: str,
    *,
    include_lorebooks: bool = True,
    leak: bool = False,
    leak_config: str | None = None,
    leak_model: str | None = None,
    leak_mode: str = "user",
    leak_prompt: str | None = None,
    leak_system: str | None = None,
    leak_keep: bool = False,
    leak_echo: bool = True,
    verbose: int = 0,
) -> None:
    """Shared implementation for [rip saucepan extract] and [rip extract <sp url>]."""
    log = (lambda m: console.print(f"[dim]  · {m}[/]")) if verbose >= 1 else (lambda m: None)
    sp.set_trace_level(verbose)
    if leak and leak_config and leak_model:
        console.print(
            "[yellow]![/] both --leak-config and --leak-model given; using --leak-config"
        )
        leak_model = None
    # Resolve the BYOK config (by name or id) up front so we fail fast with a
    # helpful list rather than mid-extraction.
    if leak and not leak_model:
        if leak_config:
            resolved = sp.resolve_provider_config(leak_config)
            if not resolved:
                _no(
                    f"no provider config matching [bold]{leak_config}[/] - see [bold]rip saucepan providers[/]"
                )
                raise SystemExit(1)
            leak_config = resolved
        else:
            configs = _rank_leak_configs(sp.list_provider_configs())
            if not configs:
                _no(
                    "no BYOK provider config for --leak - add one on saucepan.ai, or pass --leak-model"
                )
                raise SystemExit(1)
            leak_config = configs[0].get("config_id")
            console.print(
                f"[dim]leak model: {configs[0].get('config_name')} ({configs[0].get('model_id')})[/]"
            )

    # Optionally set the provider's system prompt for the leak, restoring it after.
    restore: tuple[str, str | None] | None = None
    if leak and leak_system:
        if not leak_config:
            _no("--leak-system needs a BYOK --leak-config (not --leak-model)")
            raise SystemExit(1)
        try:
            previous = sp.set_provider_prompt(leak_config, leak_system)
            restore = (leak_config, previous)
            log("set provider system prompt for leak")
        except sp.SaucepanError as exc:
            _no(f"could not set --leak-system: {exc}")
            raise SystemExit(1)

    started = time.monotonic()
    try:
        result = sp.extract_companion(
            url,
            include_lorebooks=include_lorebooks,
            leak=leak,
            leak_config=leak_config,
            leak_model=leak_model,
            leak_mode=leak_mode,
            leak_prompt=leak_prompt,
            leak_keep=leak_keep,
            leak_echo=leak_echo,
            log=log,
        )
    except sp.SaucepanError as exc:
        _no(str(exc))
        if exc.status == 401:
            err_console.print("[dim]run [bold]rip saucepan login[/] to authenticate[/]")
        raise SystemExit(1)
    finally:
        sp.set_trace_level(0)
        if restore is not None:
            try:
                sp.set_provider_prompt(restore[0], restore[1])
                log("restored provider system prompt")
            except sp.SaucepanError:
                err_console.print(
                    "[yellow]![/] could not restore the provider system prompt — "
                    f"check config [bold]{leak_config}[/] in Saucepan settings"
                )
    elapsed = time.monotonic() - started
    library_dir = OUT / "library"
    paths = save_to_library(library_dir, result.get("characterId") or "", result)

    # Persist the raw leaked dump next to the card — the parsed merge is lossy,
    # so keeping the verbatim text lets you review or hand-fix it.
    leak_raw = result.get("leakRaw") or ""
    leak_path = None
    if leak_raw:
        leak_path = library_dir / f"{result.get('characterId') or 'card'}.leak.txt"
        leak_path.write_text(leak_raw, encoding="utf-8")

    _ok(f"extracted [bold]{result.get('characterName') or url}[/] [dim](saucepan)[/]")
    _path("card png", paths["png"])
    if leak_path is not None:
        _path("raw leak", leak_path)
    character = result.get("character") or {}
    diagnostics = result.get("diagnostics") or {}
    greetings = (1 if character.get("firstMessage") else 0) + len(
        character.get("alternateGreetings") or []
    )
    _field("greetings", greetings)
    _field(
        "lorebook entries",
        f"{diagnostics.get('lorebookEntries', 0)} in {diagnostics.get('lorebooks', 0)} book(s)",
    )
    source = character.get("definitionSource")
    if source == "saucepan-echo":
        _field(
            "definition",
            f"[green]leaked {diagnostics.get('leakChars', 0)} chars verbatim via echo proxy[/]",
        )
    elif source == "saucepan-leak":
        _field(
            "definition",
            f"[green]leaked {diagnostics.get('leakChars', 0)} chars via model[/] [dim](lossy)[/]",
        )
    elif leak and diagnostics.get("leakError"):
        _field(
            "definition",
            f"[yellow]leak failed: {diagnostics['leakError']} - kept public data[/]",
        )
    elif source == "saucepan-partial":
        _field(
            "definition",
            "[yellow]partial - definition gated, body/greetings from public data[/]",
        )
    _field("time", _fmt_duration(elapsed))

    if verbose:
        _field("definition open", diagnostics.get("definitionOpen"))
        _field("definition sections", diagnostics.get("sections") or [])
        _field("lorebooks", diagnostics.get("lorebooks", 0))
        if leak and diagnostics.get("leakError"):
            _field("leak error", diagnostics["leakError"])


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
    "scenario, greetings, tags, avatar; the definition stays gated). Marked clank-partial.",
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
        library_dir = OUT / "library"
        saved = 0
        for it in items:
            try:
                result = ck.extract_story(it)
                save_to_library(library_dir, result.get("characterId") or "", result)
                saved += 1
            except ck.ClankError as exc:
                err_console.print(f"[yellow]![/] {it.get('agent_name')}: {exc}")
        _ok(f"saved [bold]{saved}[/] partial card(s) to {library_dir} [dim](clank-partial)[/]")


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


def _clank_extract(
    url: str,
    *,
    leak: bool = False,
    keep_boilerplate: bool = False,
    trigger_message: str = "hi",
    with_lorebook: bool = False,
    max_triggers: int = 8,
    verbose: int = 0,
) -> None:
    """Shared implementation for [rip clank extract] and [rip extract <clank url>]."""
    log = (lambda m: console.print(f"[dim]  · {m}[/]")) if verbose >= 1 else (lambda m: None)
    ck.set_trace_level(verbose)
    started = time.monotonic()
    try:
        result = ck.extract_chat(
            url,
            leak=leak,
            keep_boilerplate=keep_boilerplate,
            trigger_message=trigger_message,
            with_lorebook=with_lorebook,
            max_triggers=max_triggers,
            log=log,
        )
    except ck.ClankError as exc:
        _no(str(exc))
        if exc.status == 401:
            err_console.print("[dim]run [bold]rip clank login[/] to authenticate[/]")
        raise SystemExit(1)
    finally:
        ck.set_trace_level(0)
    elapsed = time.monotonic() - started

    library_dir = OUT / "library"
    paths = save_to_library(library_dir, result.get("characterId") or "", result)

    leak_raw = result.get("leakRaw") or ""
    leak_path = None
    if leak_raw:
        leak_path = library_dir / f"{result.get('characterId') or 'card'}.leak.txt"
        leak_path.write_text(leak_raw, encoding="utf-8")

    _ok(f"extracted [bold]{result.get('characterName') or url}[/] [dim](clank)[/]")
    _path("card png", paths["png"])
    if leak_path is not None:
        _path("raw leak", leak_path)

    character = result.get("character") or {}
    diagnostics = result.get("diagnostics") or {}
    source = character.get("definitionSource")
    if source == "clank-echo-leak":
        _field(
            "definition",
            f"[green]leaked {diagnostics.get('definitionChars', 0)} chars verbatim via echo proxy[/]",
        )
    elif diagnostics.get("leakError"):
        _field("definition", f"[yellow]not leaked: {diagnostics['leakError']}[/]")
    else:
        _field(
            "definition",
            "[yellow]partial - no echo in chat; configure the proxy + send a message, or use --leak[/]",
        )
    greetings = (1 if character.get("firstMessage") else 0) + len(character.get("alternateGreetings") or [])
    _field("greetings", greetings)
    _field("scenario", f"{diagnostics.get('scenarioChars', 0)} chars")
    _field("example dialogue", f"{diagnostics.get('exampleChars', 0)} chars")
    tags = diagnostics.get("tags") or []
    if tags:
        _field("tags", ", ".join(tags))
    if with_lorebook:
        if "lorebookEntries" in diagnostics:
            n = diagnostics["lorebookEntries"]
            _field(
                "lorebook",
                f"[green]{n} entr{'y' if n == 1 else 'ies'} recovered via triggers[/]"
                if n else "no lorebook entries fired (character may have none)",
            )
        elif diagnostics.get("lorebookError"):
            _field("lorebook", f"[yellow]not run: {diagnostics['lorebookError']}[/]")
    _field("time", _fmt_duration(elapsed))


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
        "definition is public, else a spicychat-partial card).",
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
        library_dir = OUT / "library"
        saved = 0
        for doc in hits:
            cid = str(doc.get("character_id") or "")
            try:
                res = sc.extract_character(cid)
                save_to_library(library_dir, res.get("characterId") or cid, res)
                saved += 1
            except sc.SpicyChatError as exc:
                err_console.print(f"[yellow]![/] {doc.get('name')}: {exc}")
        _ok(f"saved [bold]{saved}[/] card(s) to {library_dir}")


@spicychat.command("extract")
@click.argument("url", metavar="URL_OR_UUID")
@verbose_option
def spicychat_extract(url: str, verbose: int) -> None:
    """Rip a spicychat.ai character card from a chatbot URL or UUID.

    URL can be a [cyan]spicychat.ai/chatbot/<uuid>[/] URL, a
    [cyan]spicychat.ai/characters/<uuid>[/] URL, or a bare UUID. No login is
    required for a character whose definition is public.
    """
    _spicychat_extract(url, verbose=verbose)


def _spicychat_extract(url: str, *, verbose: int = 0) -> None:
    """Shared impl for [rip spicychat extract] and [rip extract <spicychat url>]."""
    log = (lambda m: console.print(f"[dim]  · {m}[/]")) if verbose >= 1 else (lambda m: None)
    sc.set_trace_level(verbose)
    started = time.monotonic()
    try:
        result = sc.extract_character(url, log=log)
    except sc.SpicyChatError as exc:
        _no(str(exc))
        raise SystemExit(1)
    finally:
        sc.set_trace_level(0)
    elapsed = time.monotonic() - started

    library_dir = OUT / "library"
    paths = save_to_library(library_dir, result.get("characterId") or "", result)

    _ok(f"extracted [bold]{result.get('characterName') or url}[/] [dim](spicychat)[/]")
    _path("card png", paths["png"])

    character = result.get("character") or {}
    diagnostics = result.get("diagnostics") or {}
    if character.get("definitionSource") == "spicychat-api":
        _field("definition", f"[green]public — {diagnostics.get('definitionChars', 0)} chars[/]")
    else:
        _field(
            "definition",
            "[yellow]gated (definition_visible=false) — partial card (greeting + metadata)[/]",
        )
    _field("greeting", f"{diagnostics.get('greetingChars', 0)} chars")
    _field("scenario", f"{diagnostics.get('scenarioChars', 0)} chars")
    _field("example dialogue", f"{diagnostics.get('exampleChars', 0)} chars")
    tags = diagnostics.get("tags") or []
    if tags:
        _field("tags", ", ".join(tags))
    if diagnostics.get("lorebookCount"):
        _field("lorebooks", f"{diagnostics['lorebookCount']} attached [dim](entries gated)[/]")
    _field("time", _fmt_duration(elapsed))


def _report_open_card(result: dict, *, platform: str, url: str, elapsed: float) -> None:
    """Shared result printer for the open-archive rippers (chub, tavern)."""
    library_dir = OUT / "library"
    paths = save_to_library(library_dir, result.get("characterId") or "card", result)

    _ok(f"extracted [bold]{result.get('characterName') or url}[/] [dim]({platform})[/]")
    _path("card png", paths["png"])

    diagnostics = result.get("diagnostics") or {}
    _field("definition", f"[green]public — {diagnostics.get('descriptionChars', 0)} chars[/]")
    _field("first message", f"{diagnostics.get('firstMessageChars', 0)} chars")
    _field("example dialogue", f"{diagnostics.get('exampleChars', 0)} chars")
    if diagnostics.get("alternateGreetings"):
        _field("alt greetings", diagnostics["alternateGreetings"])
    if diagnostics.get("lorebookEntries"):
        _field("lorebook", f"{diagnostics['lorebookEntries']} entries [dim](keys preserved)[/]")
    tags = diagnostics.get("tags") or []
    if tags:
        _field("tags", ", ".join(tags[:8]))
    _field("time", _fmt_duration(elapsed))


def _chub_extract(url: str, *, verbose: int = 0) -> None:
    """Shared impl for [rip extract <chub url>]."""
    log = (lambda m: console.print(f"[dim]  · {m}[/]")) if verbose >= 1 else (lambda m: None)
    cb.set_trace_level(verbose)
    started = time.monotonic()
    try:
        result = cb.extract_character(url, log=log)
    except cb.ChubError as exc:
        _no(str(exc))
        raise SystemExit(1)
    finally:
        cb.set_trace_level(0)
    _report_open_card(result, platform="chub", url=url, elapsed=time.monotonic() - started)


def _tavern_extract(url: str, *, verbose: int = 0) -> None:
    """Shared impl for [rip extract <card-file / character-tavern url>]."""
    log = (lambda m: console.print(f"[dim]  · {m}[/]")) if verbose >= 1 else (lambda m: None)
    tv.set_trace_level(verbose)
    started = time.monotonic()
    try:
        result = tv.extract_card(url, log=log)
    except tv.TavernCardError as exc:
        _no(str(exc))
        raise SystemExit(1)
    finally:
        tv.set_trace_level(0)
    platform = result.get("diagnostics", {}).get("cardKind") or "card"
    _report_open_card(result, platform=f"tavern-{platform}", url=url, elapsed=time.monotonic() - started)


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


if __name__ == "__main__":
    main()
