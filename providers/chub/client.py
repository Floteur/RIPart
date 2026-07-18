"""chub.ai / CharacterHub HTTP client: URL parsing, node fetch, card + avatar.

chub.ai (a.k.a. characterhub.org / charhub.io) is an *open* archive: a public
character's full definition is served with no auth. Two read surfaces are used:

* ``GET https://api.chub.ai/api/characters/<fullPath>?full=true`` — the richest
  record: structured ``definition`` (description, first message, example
  dialogue, alternate greetings, embedded lorebook) plus metadata (tags via
  ``topics``, token count, avatar URL, nsfw flag).
* ``https://avatars.charhub.io/avatars/<fullPath>/chara_card_v2.png`` — the card
  as a downloadable PNG, used as a fallback when the API omits the definition.

A ``fullPath`` is ``<creator>/<slug>`` (chub's stable identifier). Built on the
shared :class:`~ripart.common.http.HttpClient`.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ...common.avatar import fetch_avatar as _fetch_avatar
from ...common.errors import RipError
from ...common.http import HttpClient

API_BASE = "https://api.chub.ai"
AVATARS_BASE = "https://avatars.charhub.io"
CHUB_ORIGIN = "https://chub.ai"

CHUB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = 30

# Hosts that serve the same chub archive under different brands.
_CHUB_HOSTS = ("chub.ai", "characterhub.org", "charhub.io")


class ChubError(RipError):
    """User-facing chub.ai failure; carries an optional HTTP-ish status code."""


_http = HttpClient(
    base_url=API_BASE,
    user_agent=CHUB_UA,
    trace_name="chub-http",
    error_label="chub.ai",
    error_cls=ChubError,
    timeout=TIMEOUT,
)


def set_trace_level(level: int) -> None:
    _http.set_trace_level(level)


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #


def is_chub_url(url: str) -> bool:
    host = urlparse(url if "//" in (url or "") else f"//{url}").netloc.lower()
    return any(host == h or host.endswith(f".{h}") for h in _CHUB_HOSTS)


def parse_full_path(url: str) -> str | None:
    """Extract a ``<creator>/<slug>`` fullPath from a chub URL (or a bare path).

    Accepts ``chub.ai/characters/<creator>/<slug>`` (and the ``characters/``,
    ``lorebooks/`` are stripped), any of the mirror hosts, or a bare
    ``<creator>/<slug>``. Returns ``None`` when no two-segment path is present.
    """
    text = (url or "").strip()
    if not text:
        return None
    if is_chub_url(text):
        path = urlparse(text if "//" in text else f"//{text}").path
    else:
        # A bare "creator/slug" (no host) is accepted as a fullPath.
        path = text if "/" in text and "://" not in text and " " not in text else ""
    segments = [s for s in path.split("/") if s]
    # Drop a leading collection segment ("characters"/"character"/"lorebooks").
    if segments and segments[0].lower() in ("characters", "character", "lorebooks"):
        segments = segments[1:]
    if len(segments) < 2:
        return None
    return "/".join(segments[:2])


def character_url(full_path: str) -> str:
    return f"{CHUB_ORIGIN}/characters/{full_path}"


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

_RETRY_ATTEMPTS = 3


def _headers() -> dict[str, str]:
    return {
        "User-Agent": CHUB_UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": CHUB_ORIGIN,
        "Referer": f"{CHUB_ORIGIN}/",
    }


def fetch_node(full_path: str) -> dict:
    """Return the full character node from ``GET /api/characters/<path>?full=true``.

    Raises :class:`ChubError` on a missing character or a non-JSON/error
    response.
    """
    response = _http.send(
        "GET",
        f"/api/characters/{full_path}?full=true",
        headers=_headers(),
        attempts=_RETRY_ATTEMPTS,
        retry_5xx=True,
        trace_label=f"characters/{full_path}",
    )
    if response.status_code == 404:
        raise ChubError(f"character not found: {full_path}", 404)
    if response.is_error:
        raise ChubError(
            f"chub.ai returned {response.status_code} for {full_path}",
            response.status_code,
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise ChubError(f"chub.ai returned non-JSON ({response.status_code})") from exc
    node = data.get("node") if isinstance(data, dict) else None
    if not isinstance(node, dict):
        raise ChubError("unexpected chub.ai character response shape")
    return node


def fetch_card_png(full_path: str) -> bytes | None:
    """Download the character's ``chara_card_v2.png`` (embedded card), or ``None``."""
    try:
        response = _http.client().get(
            f"{AVATARS_BASE}/avatars/{full_path}/chara_card_v2.png",
            headers={
                "User-Agent": CHUB_UA,
                "Accept": "image/*,*/*;q=0.8",
                "Referer": f"{CHUB_ORIGIN}/",
            },
        )
    except Exception:
        return None
    if response.is_error or not response.content:
        return None
    return response.content


def fetch_avatar(image_url: str | None) -> str:
    """Download an avatar URL as a ``data:`` URI (empty string on failure)."""
    if not image_url:
        return ""
    return _fetch_avatar(_http, str(image_url), referer=f"{CHUB_ORIGIN}/")
