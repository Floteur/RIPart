"""clank.world HTTP client: session cookies, headers, URL parsing, avatars.

Auth is the browser's ``next-auth`` session cookie (a JWE), persisted to a
gitignored ``.clank-session.json`` at the package root and reused by every
command. Mutations additionally need the CSRF cookie. Built on
:mod:`ripart.common.http` (pooled client + retry + tracing) and
:mod:`ripart.common.creds` (the persisted session with a per-thread override).
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path

import httpx

from ...common.avatar import fetch_avatar as _fetch_avatar
from ...common.creds import CredentialStore
from ...common.errors import RipError
from ...common.http import HttpClient

CLANK_BASE = "https://www.clank.world"
CLANK_ORIGIN = "https://www.clank.world"
CLANK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = 30

# Session cookies live at the package root, gitignored. A small JSON object:
# {"session_token": "...", "csrf_token": "..."}. Never printed back to the user.
SESSION_FILE = Path(__file__).resolve().parents[2] / ".clank-session.json"


class ClankError(RipError):
    """User-facing clank.world failure; carries an optional HTTP-ish status code."""


_http = HttpClient(
    base_url=CLANK_BASE,
    user_agent=CLANK_UA,
    trace_name="clank-http",
    error_label="clank.world",
    error_cls=ClankError,
    timeout=TIMEOUT,
)
_creds = CredentialStore(
    SESSION_FILE, empty={}, loads=json.loads, dumps=json.dumps
)


def _get_client() -> httpx.Client:
    """The shared pooled client (kept as a function for call-site compatibility)."""
    return _http.client()


def set_trace_level(level: int) -> None:
    _http.set_trace_level(level)


# --------------------------------------------------------------------------- #
# Persisted session cookies (+ per-thread override)
# --------------------------------------------------------------------------- #


def _active_session() -> dict[str, str]:
    """The session cookies for the calling thread: its override, else the global."""
    return _creds.active() or {}


@contextmanager
def use_session(session: dict[str, str]):
    """Run a block with ``session`` as the active cookies for this thread only."""
    with _creds.use(dict(session or {})):
        yield


def load_session() -> dict[str, str]:
    """Read the persisted session cookies into memory (called on first use)."""
    return _creds.load() or {}


def set_session(session_token: str, csrf_token: str | None = None) -> None:
    """Store the session cookies in memory and on disk (owner-only file perms)."""
    session = {"session_token": str(session_token or "").strip()}
    if csrf_token:
        session["csrf_token"] = str(csrf_token).strip()
    _creds.store(session)


def clear_session() -> None:
    """Forget the stored session (log out)."""
    _creds.clear()


def has_session() -> bool:
    return bool(_active_session().get("session_token"))


def _cookie_header(session: dict[str, str] | None = None) -> str:
    session = session if session is not None else _active_session()
    parts = []
    if session.get("session_token"):
        parts.append(f"__Secure-next-auth.session-token={session['session_token']}")
    if session.get("csrf_token"):
        parts.append(f"__Host-next-auth.csrf-token={session['csrf_token']}")
    return "; ".join(parts)


def _headers(*, referer: str | None = None, json_body: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": CLANK_UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": CLANK_ORIGIN,
        "Referer": referer or f"{CLANK_ORIGIN}/",
    }
    cookie = _cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #

_UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_UUID_ANY = re.compile(_UUID_RE, re.I)


def parse_chat_id(url: str) -> str | None:
    """Extract the chat UUID from a ``clank.world/chat/<uuid>`` URL (or bare UUID)."""
    match = re.search(rf"clank\.world/chat/({_UUID_RE})", url or "", re.I)
    if match:
        return match.group(1)
    bare = (url or "").strip()
    if re.fullmatch(_UUID_RE, bare, re.I):
        return bare
    return None


def parse_target(url: str) -> tuple[str | None, str | None]:
    """Classify a clank URL: ``("chat", chat_id)`` or ``("character", slug)``.

    Accepts ``clank.world/chat/<uuid>`` (or a bare UUID) and character pages
    ``clank.world/@<slug>`` (e.g. ``@c/physical-longer-top``). Returns
    ``(None, None)`` if neither matches.
    """
    chat_id = parse_chat_id(url)
    if chat_id:
        return "chat", chat_id
    match = re.search(r"clank\.world/@([^?#\s]+)", url or "", re.I)
    if match:
        return "character", match.group(1).rstrip("/")
    return None, None


def parse_scene_id(value: str) -> str | None:
    """Extract a scene UUID from a ``?scene=<uuid>`` URL, a story item, or bare UUID."""
    match = re.search(r"[?&]scene=(" + _UUID_RE + r")", value or "", re.I)
    if match:
        return match.group(1)
    bare = (value or "").strip()
    return bare if re.fullmatch(_UUID_RE, bare, re.I) else None


def is_clank_url(url: str) -> bool:
    return "clank.world" in (url or "").lower()


def _fetch_html(path: str) -> str:
    """GET a clank.world page as text (authenticated), '' on error."""
    try:
        response = _http.client().get(
            path,
            headers={
                "User-Agent": CLANK_UA,
                "Accept": "text/html,*/*",
                "Cookie": _cookie_header(),
                "Referer": f"{CLANK_ORIGIN}/",
            },
        )
        return "" if response.is_error else response.text
    except httpx.HTTPError:
        return ""


def fetch_avatar(image: str | None) -> str:
    """Download an avatar (URL or path) as a ``data:`` URI (or '' on failure)."""
    if not image:
        return ""
    url = (
        image
        if str(image).startswith(("http://", "https://"))
        else f"{CLANK_BASE}/{str(image).lstrip('/')}"
    )
    return _fetch_avatar(_http, url, referer=f"{CLANK_ORIGIN}/")
