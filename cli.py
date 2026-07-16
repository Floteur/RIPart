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

from .browser_tasks import (
    extract_task,
    import_session_task,
    inspect_task,
    login_task,
    recent_task,
    status_task,
)
from . import saucepan as sp
from .helpers import safe_name, save_to_library, write_json

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
        {"name": "Setup", "commands": ["completion"]},
    ],
    "rip saucepan": [
        {"name": "Session & login", "commands": ["login", "status", "logout"]},
        {"name": "Ripping", "commands": ["extract", "providers"]},
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
@click.version_option(package_name="ripart", prog_name="rip", message="%(prog)s %(version)s")
def main() -> None:
    """[bold]RIPart[/] - rip characters & lorebooks from JanitorAI.

    A small browser-driven CLI (powered by Botasaurus). Typical flow:

    \b
      1. rip login                     log in once (reused afterwards)
      2. rip status                    confirm you are logged in
      3. rip inspect <url>             peek at a character's public metadata
      4. rip extract <url>             rip the full card + lorebook

    Results are written under [cyan]output/cli/[/]. Run [bold]rip COMMAND --help[/]
    for details on any command.
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
@click.option("--verbose", is_flag=True, default=False, help="Print non-secret import diagnostics.")
@headed_option
def import_session(
    path: Path,
    refresh_wait: int,
    check_timeout: int,
    bypass_cloudflare: bool,
    verbose: bool,
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
            _no("blocked by Cloudflare challenge - retry with [bold]--bypass-cloudflare[/]")

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
    name = safe_name(result.get("characterName") or result.get("characterId") or "", "character")
    path = write_json(OUT / "inspections" / f"{name}.json", result)
    _ok(f"inspected [bold]{result.get('characterName') or result.get('characterId')}[/]")
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
@click.option("--verbose", is_flag=True, default=False, help="Print non-secret extraction diagnostics.")
@headed_option
def extract(
    url: str,
    delete_chat_on_error: bool,
    max_trigger_passes: int,
    trigger_chunk_size: int,
    trigger_settle_ms: int,
    no_multi_trigger: bool,
    jllm_leak: bool,
    verbose: bool,
    headed: bool,
) -> None:
    """Rip a character's private card + lorebook via generateAlpha.

    URL can be a full JanitorAI character URL or just its UUID. Requires an
    active login (see [bold]rip login[/]). Works entirely through direct API
    calls (no chat UI), so it is fast. Stores a single self-contained card PNG
    (V3 card + embedded lorebook) at [cyan]output/cli/library/<uuid>.png[/].

    Paste a [bold]saucepan.ai/companion/<id>[/] URL and it is ripped directly
    through Saucepan's API (no browser); see [bold]rip saucepan[/].
    """
    if sp.is_saucepan_url(url):
        _saucepan_extract(url)
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

    _ok(f"extracted [bold]{result.get('characterName') or result.get('characterId') or url}[/]")
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
@click.option("--verbose", is_flag=True, default=False, help="Print progress diagnostics.")
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
    verbose: bool,
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
    existing = [path.stem for path in library_dir.glob("*.png")] if library_dir.exists() else []

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
        console.print(f"\n[dim]run again with [bold]--extract[/] to rip these cards · {_fmt_duration(elapsed)}[/]")
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
            console.print(f"[dim]↷ {entry.get('name')} - already extracted (use --force)[/]")
            continue
        if entry.get("forbidden"):
            console.print(f"[dim]⊘ {entry.get('name')} - proxies disabled (pass --jllm-leak to reconstruct)[/]")
            continue
        if not entry.get("ok"):
            _no(f"{entry.get('name')} - {entry.get('error')}")
            continue
        result = entry.get("result") or {}
        paths = save_to_library(OUT / "library", result.get("characterId") or "", result)
        secs = entry.get("seconds")
        timing = f" [dim]({secs}s)[/]" if secs is not None else ""
        tag = " [yellow](jllm-reconstructed)[/]" if entry.get("reconstructed") else ""
        _ok(f"{result.get('characterName') or entry.get('name')}{tag} - {entry.get('entries', 0)} entries{timing} → [cyan]{paths['png']}[/]")


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
@click.option("--username", prompt=True, help="Your Saucepan username (prompted if omitted).")
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
    type=click.Choice(["director", "user"]),
    default="director",
    show_default=True,
    help="[--leak] Generation mode; 'director' (OOC) usually complies best.",
)
def saucepan_extract(
    url: str,
    no_lorebooks: bool,
    leak: bool,
    leak_config: str | None,
    leak_model: str | None,
    leak_mode: str,
) -> None:
    """Rip a Saucepan companion card + lorebooks by URL (or bare companion id)."""
    _saucepan_extract(
        url,
        include_lorebooks=not no_lorebooks,
        leak=leak,
        leak_config=leak_config,
        leak_model=leak_model,
        leak_mode=leak_mode,
    )


def _saucepan_extract(
    url: str,
    *,
    include_lorebooks: bool = True,
    leak: bool = False,
    leak_config: str | None = None,
    leak_model: str | None = None,
    leak_mode: str = "director",
) -> None:
    """Shared implementation for [rip saucepan extract] and [rip extract <sp url>]."""
    # Resolve the BYOK config (by name or id) up front so we fail fast with a
    # helpful list rather than mid-extraction.
    if leak and not leak_model:
        if leak_config:
            resolved = sp.resolve_provider_config(leak_config)
            if not resolved:
                _no(f"no provider config matching [bold]{leak_config}[/] - see [bold]rip saucepan providers[/]")
                raise SystemExit(1)
            leak_config = resolved
        else:
            configs = [c for c in sp.list_provider_configs() if c.get("is_visible")]
            if not configs:
                _no("no BYOK provider config for --leak - add one on saucepan.ai, or pass --leak-model")
                raise SystemExit(1)
            leak_config = configs[0].get("config_id")
            console.print(f"[dim]leak model: {configs[0].get('config_name')} ({configs[0].get('model_id')})[/]")

    started = time.monotonic()
    try:
        result = sp.extract_companion(
            url,
            include_lorebooks=include_lorebooks,
            leak=leak,
            leak_config=leak_config,
            leak_model=leak_model,
            leak_mode=leak_mode,
        )
    except sp.SaucepanError as exc:
        _no(str(exc))
        if exc.status == 401:
            err_console.print("[dim]run [bold]rip saucepan login[/] to authenticate[/]")
        raise SystemExit(1)
    elapsed = time.monotonic() - started
    paths = save_to_library(OUT / "library", result.get("characterId") or "", result)

    _ok(f"extracted [bold]{result.get('characterName') or url}[/] [dim](saucepan)[/]")
    _path("card png", paths["png"])
    character = result.get("character") or {}
    diagnostics = result.get("diagnostics") or {}
    greetings = (1 if character.get("firstMessage") else 0) + len(character.get("alternateGreetings") or [])
    _field("greetings", greetings)
    _field("lorebook entries", f"{diagnostics.get('lorebookEntries', 0)} in {diagnostics.get('lorebooks', 0)} book(s)")
    source = character.get("definitionSource")
    if source == "saucepan-leak":
        _field("definition", f"[green]leaked {diagnostics.get('leakChars', 0)} chars via model[/] [dim](lossy)[/]")
    elif leak and diagnostics.get("leakError"):
        _field("definition", f"[yellow]leak failed: {diagnostics['leakError']} - kept public data[/]")
    elif source == "saucepan-partial":
        _field("definition", "[yellow]partial - definition gated, body/greetings from public data[/]")
    _field("time", _fmt_duration(elapsed))


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
