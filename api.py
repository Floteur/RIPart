"""High-level Python API for RIPart.

Import this in your own scripts instead of shelling out to the ``rip`` CLI:

    >>> import ripart
    >>> ripart.login()                      # one-time, opens a browser
    >>> ripart.is_logged_in()
    True
    >>> result = ripart.extract("https://janitorai.com/characters/<uuid>_name")
    >>> result["characterName"]
    'Elena Vasquez'
    >>> result["savedPath"]                 # self-contained V3 card PNG
    'ripart-output/library/<uuid>.png'

Every function returns a plain ``dict`` (the same shape the CLI prints), so the
data is easy to inspect, serialise, or feed into ``save()`` yourself. Saucepan
companion URLs are handled transparently by :func:`extract`; the lower-level
Saucepan helpers live under ``ripart.providers.saucepan``.

The browser-driven calls (:func:`login`, :func:`extract`, :func:`recent`,
:func:`inspect`, :func:`import_session`, :func:`is_logged_in`) run a real,
headless Chromium via Botasaurus and reuse a persistent profile, so a single
:func:`login` keeps you authenticated across later calls and across processes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .common.cards import save_to_library
from .providers import clank, saucepan
from .providers.clank import ClankError
from .providers.janitor import (
    extract_task,
    import_session_task,
    inspect_task,
    login_task,
    recent_task,
    status_task,
)
from .providers.saucepan import SaucepanError

__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "ClankError",
    "SaucepanError",
    "clank",
    "extract",
    "import_session",
    "inspect",
    "is_logged_in",
    "login",
    "recent",
    "save",
    "saucepan",
]

# Where :func:`extract`/:func:`recent` write ripped cards by default. Relative to
# the current working directory so it lands in the caller's project, not inside
# the installed package. Override per-call with ``output_dir=...``.
DEFAULT_OUTPUT_DIR = Path("ripart-output")


def _browser_kwargs(headless: bool) -> dict[str, bool]:
    return {"headless": headless, "enable_xvfb_virtual_display": False}


def _library_dir(output_dir: Path | str) -> Path:
    return Path(output_dir) / "library"


# --------------------------------------------------------------------------- #
# Session & login
# --------------------------------------------------------------------------- #


def is_logged_in(*, headless: bool = True) -> bool:
    """Return ``True`` if the stored JanitorAI session is still authenticated."""
    result = status_task({}, **_browser_kwargs(headless))
    return bool(result.get("loggedIn"))


def login(*, timeout: int = 180, headless: bool = False) -> dict[str, Any]:
    """Log into JanitorAI, persisting the session for later calls.

    Opens a visible browser by default so you can complete any Cloudflare /
    Google sign-in, then waits up to ``timeout`` seconds for the session to
    become authenticated. The session is saved to the shared profile, so you
    only need to do this once. Returns the raw task result (``{"loggedIn": ...,
    "sessionSaved": ..., "sessionFile": ...}``).
    """
    return login_task({"timeout": timeout}, **_browser_kwargs(headless))


def import_session(
    session_path: Path | str,
    *,
    refresh_wait: int = 3,
    check_timeout: int = 0,  # matches the `rip import-session` CLI default
    bypass_cloudflare: bool = False,
    verbose: int = 0,
    headless: bool = True,
) -> dict[str, Any]:
    """Import a JanitorAI session exported from your browser (cookies + storage).

    ``session_path`` points at a JSON export (e.g. from a cookie-editor
    extension). Useful on headless machines where interactive :func:`login`
    is impractical. Returns the raw task result including a login ``probe``.
    """
    return import_session_task(
        {
            "session_path": str(Path(session_path).resolve()),
            "refresh_wait": refresh_wait,
            "check_timeout": check_timeout,
            "verbose": verbose,
            "bypass_cloudflare": bypass_cloudflare,
        },
        **_browser_kwargs(headless),
    )


# --------------------------------------------------------------------------- #
# Ripping
# --------------------------------------------------------------------------- #


def inspect(url: str, *, headless: bool = True) -> dict[str, Any]:
    """Peek at a character's public metadata without ripping it.

    ``url`` may be a full JanitorAI character URL or a bare UUID. Returns public
    metadata, whether the card body is public, and any public lorebooks.
    """
    return inspect_task({"url": url}, **_browser_kwargs(headless))


def extract(
    url: str,
    *,
    save: bool = True,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    delete_chat_on_error: bool = False,
    max_trigger_passes: int = 8,
    trigger_chunk_size: int = 2500,
    trigger_settle_ms: int = 0,
    multi_trigger: bool = True,
    jllm_leak: bool = False,
    verbose: int = 0,
    headless: bool = True,
    # Saucepan-only options (used when ``url`` is a saucepan.ai companion URL):
    include_lorebooks: bool = True,
    leak: bool = False,
    leak_config: str | None = None,
    leak_model: str | None = None,
    leak_mode: str = "user",
    leak_prompt: str | None = None,
    leak_keep: bool = False,
    leak_timeout: int = 180,
    # clank-only options (used when ``url`` is a clank.world chat URL):
    clank_keep_boilerplate: bool = False,
    clank_trigger_message: str = "hi",
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Rip a character's full card + lorebook and (by default) save it.

    ``url`` may be a JanitorAI character URL/UUID or a ``saucepan.ai/companion/``
    URL — Saucepan companions are ripped directly through the Saucepan API (no
    browser) and honour the ``include_lorebooks`` / ``leak*`` options; the other
    keyword options apply to JanitorAI extraction.

    Requires an authenticated session (see :func:`login`) for JanitorAI, or a
    stored Saucepan token (see ``ripart.saucepan.login``) for Saucepan URLs.

    Returns the extraction ``result`` dict. When ``save`` is true (the default)
    the card is written to ``<output_dir>/library/<uuid>.png`` (a self-contained
    V3 card + embedded lorebook) and the path is added under ``result["savedPath"]``.
    Set ``save=False`` to get the data without touching disk.

    ``verbose`` is a level, not just on/off: 1 prints progress diagnostics
    (routed through ``log`` for Saucepan URLs), 2 adds wire-level HTTP/generateAlpha
    summaries, 3 adds truncated raw request/response payload previews — useful
    for digging into a failure. Levels 2/3 print directly (they are not routed
    through ``log``).
    """
    if clank.is_clank_url(url):
        clank.set_trace_level(verbose)
        try:
            result = clank.extract_chat(
                url,
                leak=leak,
                keep_boilerplate=clank_keep_boilerplate,
                trigger_message=clank_trigger_message,
                leak_timeout=leak_timeout,
                log=log or (lambda _message: None),
            )
        finally:
            clank.set_trace_level(0)
    elif saucepan.is_saucepan_url(url):
        saucepan.set_trace_level(verbose)
        try:
            result = saucepan.extract_companion(
                url,
                include_lorebooks=include_lorebooks,
                leak=leak,
                leak_config=leak_config,
                leak_model=leak_model,
                leak_mode=leak_mode,
                leak_prompt=leak_prompt,
                leak_keep=leak_keep,
                leak_timeout=leak_timeout,
                log=log or (lambda _message: None),
            )
        finally:
            saucepan.set_trace_level(0)
    else:
        result = extract_task(
            {
                "url": url,
                "delete_chat_on_error": delete_chat_on_error,
                "verbose": verbose,
                "max_trigger_passes": 1 if not multi_trigger else max_trigger_passes,
                "trigger_chunk_size": trigger_chunk_size,
                "trigger_settle_ms": trigger_settle_ms,
                "jllm_leak": jllm_leak,
            },
            **_browser_kwargs(headless),
        )

    if save:
        result["savedPath"] = _write_to_library(result, output_dir)

    return result


def recent(
    *,
    limit: int = 20,
    sfw: bool = False,
    extract: bool = False,
    force: bool = False,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    max_trigger_passes: int = 8,
    trigger_chunk_size: int = 2500,
    multi_trigger: bool = True,
    delete_chat_on_error: bool = False,
    jllm_leak: bool = False,
    verbose: int = 0,
    headless: bool = True,
) -> dict[str, Any]:
    """List the most-recent characters, optionally ripping each into the library.

    Returns ``{"cards": [...], "extracted": [...] | None}``. With ``extract=True``
    every listed card is full-ripped into ``<output_dir>/library`` (cards already
    present are skipped unless ``force=True``); ``extracted`` is ``None`` when
    ``extract`` is false.
    """
    library_dir = _library_dir(output_dir)
    existing = (
        [path.stem for path in library_dir.glob("*.png")]
        if library_dir.exists()
        else []
    )
    return recent_task(
        {
            "limit": limit,
            "sfw": sfw,
            "extract": extract,
            "force": force,
            "existing": existing,
            "verbose": verbose,
            "max_trigger_passes": 1 if not multi_trigger else max_trigger_passes,
            "trigger_chunk_size": trigger_chunk_size,
            "delete_chat_on_error": delete_chat_on_error,
            "jllm_leak": jllm_leak,
        },
        **_browser_kwargs(headless),
    )


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #


def _write_to_library(result: dict[str, Any], output_dir: Path | str) -> str:
    paths = save_to_library(
        _library_dir(output_dir), result.get("characterId") or "", result
    )
    return paths["png"]


def save(result: dict[str, Any], *, output_dir: Path | str = DEFAULT_OUTPUT_DIR) -> str:
    """Write an extraction ``result`` to the library and return the PNG path.

    Stores a single self-contained card PNG at ``<output_dir>/library/<uuid>.png``
    (V3 card + embedded lorebook) and updates ``<output_dir>/library/index.json``.
    Accepts any ``result`` returned by :func:`extract` (JanitorAI or Saucepan).
    """
    return _write_to_library(result, output_dir)
