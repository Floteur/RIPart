"""spicychat.ai HTTP client: guest id, refresh-token auth, URL parsing, avatars.

spicychat is a NextDayAI ("nd-api") platform. Every request carries an
``x-app-id: spicychat`` header and a stable ``x-guest-userid`` (a UUID we
generate once and persist) — that guest identity alone is enough to read a
character's *public* definition, so extraction needs no login.

Logging in is optional and adds an ``Authorization: Bearer`` header. Auth is a
Kinde OAuth **refresh token** (copied from the browser); the client mints
short-lived access tokens from it on demand and persists the rotated refresh
token back. Note that authenticating does **not** unlock a gated definition
(``definition_visible: false``) — those stay server-side regardless — so login
mainly matters for NSFW visibility and rate limits.

Session state (guest id + tokens) lives in RIPart's owner-only application-state
directory, with a per-thread override so the leak bench can drive several
accounts from one process. Built on the shared
:class:`~ripart.common.http.HttpClient` and :class:`~ripart.common.creds.CredentialStore`.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import httpx

from ...common.avatar import fetch_avatar as _fetch_avatar
from ...common.creds import CredentialStore
from ...common.errors import RipError
from ...common.http import HttpClient
from ...common.storage import state_path

# Hosts. The REST API, the Kinde auth server, the image CDN and the public
# Typesense search cluster are four different origins; the pooled client is
# pinned to the REST API and the others are reached with absolute URLs.
NDAPI_BASE = "https://prod.nd-api.com"
AUTH_BASE = "https://auth.spicychat.ai"
CDN_BASE = "https://cdn.nd-api.com"
SPICYCHAT_ORIGIN = "https://spicychat.ai"
# Public read-only Typesense endpoint + scoped search key baked into the web app.
TYPESENSE_BASE = "https://etmzpxgvnid370fyp.a1.typesense.net"
TYPESENSE_KEY = "STHKtT6jrC5z1IozTJHIeSN4qN9oL1s3"

APP_ID = "spicychat"
# Kinde OAuth client id (the JWT ``azp`` of a spicychat browser session).
CLIENT_ID = "fb5754f42ee84f4787f9bd8ff49cac7a"

SPICYCHAT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = 30

# Session state in the application-state directory. A small JSON object:
# {"guest_id", "refresh_token", "access_token", "access_expiry"}. Never printed.
_LEGACY_SESSION_FILE = Path(__file__).resolve().parents[2] / ".spicychat-session.json"
SESSION_FILE = state_path("spicychat-session.json")


class SpicyChatError(RipError):
    """User-facing spicychat.ai failure; carries an optional HTTP-ish status code."""


_http = HttpClient(
    base_url=NDAPI_BASE,
    user_agent=SPICYCHAT_UA,
    trace_name="spicychat-http",
    error_label="spicychat.ai",
    error_cls=SpicyChatError,
    timeout=TIMEOUT,
)
_creds = CredentialStore(
    SESSION_FILE,
    empty={},
    loads=json.loads,
    dumps=json.dumps,
    legacy_path=_LEGACY_SESSION_FILE,
)


def set_trace_level(level: int) -> None:
    _http.set_trace_level(level)


# --------------------------------------------------------------------------- #
# Persisted session (guest id + tokens) with a per-thread override
# --------------------------------------------------------------------------- #


def _active_session() -> dict[str, str]:
    """The session for the calling thread: its override, else the global."""
    return dict(_creds.active() or {})


@contextmanager
def use_session(session: dict[str, str]):
    """Run a block with ``session`` as the active session for this thread only."""
    with _creds.use(dict(session or {})):
        yield


def load_session() -> dict[str, str]:
    """Read the persisted session into memory (called on first use)."""
    return dict(_creds.load() or {})


def _guest_id() -> str:
    """The stable guest UUID, generating and persisting one on first use."""
    session = _active_session()
    gid = str(session.get("guest_id") or "").strip()
    if not gid:
        gid = str(uuid.uuid4())
        session["guest_id"] = gid
        _creds.persist_active(session)
    return gid


def set_refresh_token(refresh_token: str) -> None:
    """Store a Kinde refresh token (log in); a guest id is created if absent."""
    session = _active_session()
    session["refresh_token"] = str(refresh_token or "").strip()
    # Drop any cached access token so the next call re-mints from the new refresh.
    session.pop("access_token", None)
    session.pop("access_expiry", None)
    if not session.get("guest_id"):
        session["guest_id"] = str(uuid.uuid4())
    _creds.store(session)


def clear_session() -> None:
    """Forget the stored session (log out) — guest id included."""
    _creds.clear()


def has_token() -> bool:
    """True if a refresh (or still-valid access) token is stored."""
    session = _active_session()
    return bool(session.get("refresh_token") or session.get("access_token"))


def token_expiry() -> float:
    """Unix expiry of the cached access token (0 if none)."""
    try:
        return float(_active_session().get("access_expiry") or 0)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# Bearer minting (Kinde refresh-token grant, with rotation)
# --------------------------------------------------------------------------- #


def _mint(refresh_token: str) -> dict[str, str]:
    """Exchange a refresh token for an access token (raises on failure)."""
    body = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        }
    )
    try:
        response = _http.client().post(
            f"{AUTH_BASE}/oauth2/token",
            headers={
                "User-Agent": SPICYCHAT_UA,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": SPICYCHAT_ORIGIN,
            },
            content=body,
        )
    except httpx.HTTPError as exc:
        raise SpicyChatError(f"network error talking to spicychat.ai auth: {exc}") from exc
    if response.status_code != 200:
        raise SpicyChatError(
            "refresh token rejected - run `rip spicychat login` again "
            f"({response.status_code})",
            response.status_code,
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise SpicyChatError("spicychat.ai auth returned non-JSON") from exc
    if not data.get("access_token"):
        raise SpicyChatError("spicychat.ai auth returned no access token")
    return data


def authenticate() -> dict[str, str]:
    """Force-mint a fresh access token from the stored refresh token (raises).

    Persists the rotated refresh token and the cached access token. Returns the
    raw Kinde token payload (``access_token``, ``expires_in``, ...).
    """
    session = _active_session()
    refresh = str(session.get("refresh_token") or "").strip()
    if not refresh:
        raise SpicyChatError("no spicychat.ai login - run `rip spicychat login`", 401)
    data = _mint(refresh)
    session["access_token"] = data["access_token"]
    session["access_expiry"] = time.time() + int(data.get("expires_in") or 3600)
    if data.get("refresh_token"):
        session["refresh_token"] = data["refresh_token"]
    _creds.persist_active(session)
    return data


def _bearer() -> str | None:
    """A valid access token, minting/refreshing as needed; None if not logged in.

    Best-effort: a mint failure degrades to guest access (public definitions
    still read fine) rather than raising, so a stale refresh token never blocks
    an unauthenticated extraction.
    """
    session = _active_session()
    token = str(session.get("access_token") or "")
    if token and token_expiry() > time.time() + 60:
        return token
    if not session.get("refresh_token"):
        return None
    try:
        return authenticate()["access_token"]
    except SpicyChatError:
        return None


# --------------------------------------------------------------------------- #
# Headers
# --------------------------------------------------------------------------- #


def _headers(*, json_body: bool = False, referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": SPICYCHAT_UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": SPICYCHAT_ORIGIN,
        "Referer": referer or f"{SPICYCHAT_ORIGIN}/",
        "x-app-id": APP_ID,
        "x-guest-userid": _guest_id(),
    }
    bearer = _bearer()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #

_UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def parse_character_id(url: str) -> str | None:
    """Extract a character UUID from a spicychat URL (or a bare UUID).

    Accepts ``spicychat.ai/chatbot/<uuid>`` and ``.../characters/<uuid>`` (with
    optional trailing path/query), or a bare UUID.
    """
    match = re.search(
        rf"spicychat\.ai/(?:chatbot|characters?)/({_UUID_RE})", url or "", re.I
    )
    if match:
        return match.group(1)
    bare = (url or "").strip()
    return bare if re.fullmatch(_UUID_RE, bare, re.I) else None


def is_spicychat_url(url: str) -> bool:
    return "spicychat.ai" in (url or "").lower()


def character_url(character_id: str) -> str:
    return f"{SPICYCHAT_ORIGIN}/chatbot/{character_id}"


# --------------------------------------------------------------------------- #
# Avatars
# --------------------------------------------------------------------------- #


def fetch_avatar(image: str | None) -> str:
    """Download an avatar (relative ``avatars/<file>`` or URL) as a ``data:`` URI."""
    if not image:
        return ""
    url = (
        image
        if str(image).startswith(("http://", "https://"))
        else f"{CDN_BASE}/{str(image).lstrip('/')}"
    )
    return _fetch_avatar(_http, url, referer=f"{SPICYCHAT_ORIGIN}/")
