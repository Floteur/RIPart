"""Saucepan HTTP client: auth, headers, request plumbing, URL parsing.

Built on :mod:`ripart.common.http` (pooled client + retry + tracing) and
:mod:`ripart.common.creds` (the persisted bearer token with a per-thread
override, so the multi-account leak bench can drive several tokens at once).
"""

from __future__ import annotations

import base64
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ...common.avatar import fetch_avatar as _fetch_avatar
from ...common.creds import CredentialStore
from ...common.errors import RipError
from ...common.http import HttpClient

SAUCEPAN_BASE = "https://saucepan.ai"
SAUCEPAN_ORIGIN = "https://saucepan.ai"
SAUCEPAN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
TIMEOUT = 30
_RETRY_ATTEMPTS = 3

# Token lives at the package root, gitignored (see .gitignore). One line, the
# raw bearer token; never printed back to the user. Anchored to the package root
# (two levels up from this provider module) so it stays where it has always been.
TOKEN_FILE = Path(__file__).resolve().parents[2] / ".saucepan-token"


class SaucepanError(RipError):
    """User-facing Saucepan failure; carries an optional HTTP-ish status code."""


# One pooled client + one token store for the whole Saucepan provider.
_http = HttpClient(
    base_url=SAUCEPAN_BASE,
    user_agent=SAUCEPAN_UA,
    trace_name="saucepan-http",
    error_label="Saucepan",
    error_cls=SaucepanError,
    timeout=TIMEOUT,
)
_creds = CredentialStore(TOKEN_FILE, empty="")


# --------------------------------------------------------------------------- #
# Wire tracing
# --------------------------------------------------------------------------- #


def set_trace_level(level: int) -> None:
    """Enable Saucepan HTTP wire tracing at ``level`` (0 disables it).

    Never logs the ``Authorization`` header or a sign-in password: login goes
    through :func:`authenticate`, which posts credentials directly and never
    passes through :func:`_request_json`.
    """
    _http.set_trace_level(level)


# --------------------------------------------------------------------------- #
# Persisted bearer token (+ per-thread override)
# --------------------------------------------------------------------------- #


def _active_token() -> str:
    """The bearer token for the calling thread: its override, else the global."""
    return _creds.active() or ""


@contextmanager
def use_token(token: str):
    """Run a block with ``token`` as the active bearer for this thread only.

    Nesting is supported (the previous value is restored on exit). Passing a
    falsy token falls back to the global token for the duration of the block.
    """
    with _creds.use(str(token or "").strip()):
        yield


def load_token() -> str:
    """Read the persisted token into memory (called on first use)."""
    return _creds.load() or ""


def set_token(value: str) -> None:
    """Store a token both in memory and on disk (owner-only file perms)."""
    _creds.store(str(value or "").strip())


def clear_token() -> None:
    """Forget the token (log out)."""
    _creds.clear()


def has_token() -> bool:
    return bool(_active_token())


def token_expiry(token: str | None = None) -> int | None:
    """Read the ``exp`` (unix seconds) from a JWT, without verifying it.

    Saucepan issues a JWT whose payload carries ``exp``; decoding it lets
    ``rip saucepan status`` report whether the token has actually expired rather
    than merely whether one is present. Defaults to the stored token; pass an
    explicit ``token`` to inspect a specific one (e.g. a cached account token).
    Returns None if there is no token or it is not a decodable JWT.
    """
    token = token if token is not None else load_token()
    if not token or token.count(".") < 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)  # restore base64url padding
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    return int(exp) if isinstance(exp, (int, float)) else None


def _headers(with_auth: bool = False, referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": SAUCEPAN_UA,
        "Accept": "*/*",
        # httpx auto-decompresses gzip/deflate/br (the brotli dep is declared); we
        # never negotiate zstd so there is nothing left to hand-decode.
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": SAUCEPAN_ORIGIN,
        "Referer": referer or f"{SAUCEPAN_ORIGIN}/",
        "x-saucepan-client-version": "1",
    }
    if with_auth:
        token = _active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


# --------------------------------------------------------------------------- #
# JSON request helpers
# --------------------------------------------------------------------------- #


def _request_json(
    method: str,
    path: str,
    *,
    with_auth: bool,
    json_body: dict[str, Any] | None = None,
    attempts: int = 1,
    retry_5xx: bool = False,
) -> tuple[bool, int, Any]:
    """One HTTP round trip returning ``(ok, status, parsed_json_or_None)``.

    Retries (via the shared client) network errors and, when ``retry_5xx``,
    transient 429/5xx responses. Only GETs retry — POSTs here create chats /
    generations and must not be silently repeated.
    """
    headers = _headers(with_auth)
    if json_body is not None:
        headers = {**headers, "Content-Type": "application/json"}
    response = _http.send(
        method,
        path,
        headers=headers,
        json_body=json_body,
        attempts=attempts,
        retry_5xx=retry_5xx,
    )
    try:
        data: Any = response.json()
    except ValueError:
        data = None
    # ``not is_error`` mirrors requests' ``.ok`` (status < 400).
    return not response.is_error, response.status_code, data


def _get_json(path: str, with_auth: bool) -> tuple[bool, int, Any]:
    return _request_json(
        "GET", path, with_auth=with_auth, attempts=_RETRY_ATTEMPTS, retry_5xx=True
    )


def _post_json(
    path: str, body: dict[str, Any], with_auth: bool = True
) -> tuple[bool, int, Any]:
    # Single attempt: create-chat / generate are non-idempotent.
    return _request_json("POST", path, with_auth=with_auth, json_body=body, attempts=1)


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #


def parse_companion_id(url: str) -> str | None:
    """Extract the companion UUID from a ``saucepan.ai/companion/<id>`` URL."""
    match = re.search(r"saucepan\.ai/companion/([a-f0-9-]{8,64})", url or "", re.I)
    if match:
        return match.group(1)
    # Bare id (mirrors the JanitorAI path, which accepts UUIDs directly).
    bare = (url or "").strip()
    if re.fullmatch(r"[a-f0-9-]{8,64}", bare, re.I):
        return bare
    return None


def is_saucepan_url(url: str) -> bool:
    return "saucepan.ai" in (url or "").lower()


# --------------------------------------------------------------------------- #
# Auth (login) — posts credentials directly, never traced
# --------------------------------------------------------------------------- #


def authenticate(username: str, password: str) -> str:
    """Log in with username + password and return the bearer token.

    Unlike ``login``, this does *not* persist the token to disk or the global
    store — the caller decides what to do with it. Used by multi-account tooling
    that juggles several tokens at once (see :func:`use_token`).
    """
    try:
        response = _http.client().post(
            "/api/v1/auth/sign_in_password",
            headers={
                **_headers(with_auth=False, referer=f"{SAUCEPAN_ORIGIN}/sign-in"),
                "Content-Type": "application/json",
            },
            # Saucepan's API names this field "handle" on the wire.
            json={
                "handle": str(username or "").strip(),
                "password": str(password or ""),
            },
        )
    except httpx.HTTPError as exc:
        raise SaucepanError(f"network error talking to Saucepan: {exc}") from exc

    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.is_error:
        message = (
            (data.get("error") or {}).get("message") if isinstance(data, dict) else None
        )
        raise SaucepanError(
            message or f"Saucepan HTTP {response.status_code}", response.status_code
        )

    token = None
    if isinstance(data, dict):
        token = (
            data.get("token")
            or data.get("access_token")
            or data.get("session_token")
            or data.get("sessionToken")
        )
    if not token:
        raise SaucepanError("login succeeded but no token was returned")
    return token


def login(username: str, password: str) -> str:
    """Log in with username + password; store and return the bearer token."""
    token = authenticate(username, password)
    set_token(token)
    return token


def fetch_avatar(image_id: str | None) -> str:
    """Download the companion avatar as a ``data:`` URI (or '' on failure)."""
    if not image_id:
        return ""
    url = f"/cdn/{quote(str(image_id), safe='')}/card"
    return _fetch_avatar(_http, url, referer=f"{SAUCEPAN_ORIGIN}/")


def _companion_creator(companion: dict[str, Any]) -> tuple[str, str]:
    """Best-effort (creator_name, creator_id) from a companion payload."""
    # v2 companions expose the author inline as author_handle / author_id.
    if companion.get("author_handle") or companion.get("author_id"):
        return str(companion.get("author_handle") or "").strip(), str(
            companion.get("author_id") or ""
        )
    for key in ("creator", "user", "owner", "author"):
        holder = companion.get(key)
        if isinstance(holder, dict):
            name = (
                holder.get("display_name")
                or holder.get("handle")
                or holder.get("name")
                or ""
            )
            return str(name).strip(), str(holder.get("id") or "")
    return "", ""


def search_companions(
    *,
    limit: int = 30,
    offset: int = 0,
    tags: list[str] | None = None,
    excluded_tags: list[str] | None = None,
    include_nsfw: bool = True,
) -> dict[str, Any]:
    """Browse Saucepan's newest public companions.

    The catalogue is served by the authenticated ``/api/v1/search`` endpoint.
    Its response includes the current page in ``companions`` and, when
    available, the total number of matching records in ``total_count``.
    """
    if not has_token():
        raise SaucepanError(
            "no Saucepan token configured - run `rip saucepan login` first", 401
        )
    body = {
        "text_search": "",
        "tags": list(tags or []),
        "excluded_tags": list(excluded_tags or []),
        "match_all_fandom_tags": False,
        "limit": limit,
        "offset": offset,
        "sus": include_nsfw,
        "order_by": "created",
        "asc": False,
        "match_all_tags": True,
        "hide_hidden_content": True,
        "extra_spicy": None,
        "posted_at_from": None,
        "posted_at_to": None,
    }
    ok, status, data = _post_json("/api/v1/search", body, True)
    if not ok:
        message = None
        if isinstance(data, dict):
            error = data.get("error")
            message = error.get("message") if isinstance(error, dict) else None
        raise SaucepanError(message or f"could not list companions (HTTP {status})", status)

    items: Any = data.get("companions") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SaucepanError("unexpected response while listing companions", 502)
    return {
        "companions": [item for item in items if isinstance(item, dict)],
        "total_count": data.get("total_count") if isinstance(data, dict) else None,
    }


# Re-exported so leak/lorebook/extract can share one client instance.
__all__ = [
    "SAUCEPAN_BASE",
    "SAUCEPAN_ORIGIN",
    "SAUCEPAN_UA",
    "SaucepanError",
    "authenticate",
    "clear_token",
    "fetch_avatar",
    "has_token",
    "is_saucepan_url",
    "load_token",
    "login",
    "parse_companion_id",
    "set_token",
    "set_trace_level",
    "search_companions",
    "token_expiry",
    "use_token",
]
