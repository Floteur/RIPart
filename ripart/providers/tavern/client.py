"""Generic Tavern card-file fetcher: turn any card URL into card bytes.

The "rip any website any card" catch-all. Many open sites publish a character as
a downloadable **card file** — a PNG with the card embedded in its text chunks,
a ``.charx`` (a ZIP whose ``card.json`` is a V3 card), or a raw ``.json`` card.
Given such a URL this downloads the bytes and extracts the card dict; the
:mod:`ripart.common.tavern` core then normalises it.

A small **host adapter** maps a friendly site URL to its card-file URL — today
``character-tavern.com/character/<path>`` → ``cards.character-tavern.com/<path>.png``
(the endpoint the Character Archive scraper uses). Add a site by adding a rule.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any
from urllib.parse import urlparse

from ...common.errors import RipError
from ...common.http import HttpClient
from ...common.tavern import read_card_png

TAVERN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = 45
# Card portraits can be large; keep a generous cap for the download.
MAX_CARD_BYTES = 24 * 1024 * 1024

_CARD_EXTS = (".png", ".charx", ".json")


class TavernCardError(RipError):
    """User-facing failure ripping a generic Tavern card file."""


# A pooled client; the base URL is a placeholder since every call uses an
# absolute URL (httpx honours absolute URLs regardless of ``base_url``).
_http = HttpClient(
    base_url="https://example.invalid",
    user_agent=TAVERN_UA,
    trace_name="tavern-http",
    error_label="the card host",
    error_cls=TavernCardError,
    timeout=TIMEOUT,
)


def set_trace_level(level: int) -> None:
    _http.set_trace_level(level)


# --------------------------------------------------------------------------- #
# Host adapters + URL classification
# --------------------------------------------------------------------------- #


def _host(url: str) -> str:
    return urlparse(url if "//" in (url or "") else f"//{url}").netloc.lower()


def resolve_card_url(url: str) -> str:
    """Map a friendly site URL to a direct card-file URL (identity if none apply).

    Known adapter: ``character-tavern.com/character/<path>`` →
    ``https://cards.character-tavern.com/<path>.png``.
    """
    host = _host(url)
    parsed = urlparse(url if "//" in (url or "") else f"//{url}")
    if host.endswith("character-tavern.com"):
        path = parsed.path
        marker = "/character/"
        if marker in path:
            slug = path.split(marker, 1)[1].strip("/")
            if slug:
                return f"https://cards.character-tavern.com/{slug}.png"
    return url


def is_card_url(url: str) -> bool:
    """True if this looks like a direct card file or a URL an adapter handles."""
    host = _host(url)
    if host.endswith("character-tavern.com"):
        return True
    if host.endswith("cards.character-tavern.com"):
        return True
    path = urlparse(url if "//" in (url or "") else f"//{url}").path.lower()
    return path.endswith(_CARD_EXTS)


def card_id_from_url(url: str) -> str:
    """A filesystem-safe id derived from the card URL's final path segment."""
    path = urlparse(url if "//" in (url or "") else f"//{url}").path
    stem = path.rstrip("/").rsplit("/", 1)[-1] or "card"
    for ext in _CARD_EXTS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in stem)
    return safe or "card"


# --------------------------------------------------------------------------- #
# Download + card extraction
# --------------------------------------------------------------------------- #


def download(url: str) -> tuple[bytes, str]:
    """Download ``url`` and return ``(bytes, content_type)``; raises on failure."""
    try:
        response = _http.client().get(
            url,
            headers={
                "User-Agent": TAVERN_UA,
                "Accept": "image/*,application/json,application/octet-stream,*/*;q=0.8",
            },
        )
    except Exception as exc:
        raise TavernCardError(f"could not download card from {url}: {exc}") from exc
    if response.status_code == 404:
        raise TavernCardError(f"card not found (404): {url}", 404)
    if response.is_error:
        raise TavernCardError(
            f"card host returned {response.status_code}: {url}", response.status_code
        )
    content = response.content
    if not content:
        raise TavernCardError(f"card host returned an empty body: {url}")
    if len(content) > MAX_CARD_BYTES:
        raise TavernCardError(f"card file is too large ({len(content)} bytes): {url}")
    return content, (response.headers.get("content-type") or "").lower()


def _card_from_charx(data: bytes) -> dict[str, Any] | None:
    """Read the V3 ``card.json`` out of a ``.charx`` ZIP archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            name = next(
                (n for n in archive.namelist() if n.lower().endswith("card.json")),
                None,
            )
            if not name:
                return None
            parsed = json.loads(archive.read(name).decode("utf-8", "ignore"))
    except (zipfile.BadZipFile, ValueError, KeyError):
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_card_bytes(data: bytes, content_type: str) -> tuple[dict[str, Any], str]:
    """Return ``(card_dict, kind)`` from downloaded bytes; raises if unrecognised.

    ``kind`` is one of ``"png"``, ``"charx"``, ``"json"`` (used to label the
    definition source). Detection is content-sniffed, so a mislabelled extension
    still works.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        card = read_card_png(data)
        if card is None:
            raise TavernCardError("PNG carries no embedded character card")
        return card, "png"
    if data[:2] == b"PK":  # ZIP magic → .charx
        card = _card_from_charx(data)
        if card is None:
            raise TavernCardError(".charx archive has no readable card.json")
        return card, "charx"
    stripped = data.lstrip()
    if stripped[:1] in (b"{", b"["):
        try:
            parsed = json.loads(stripped)
        except ValueError as exc:
            raise TavernCardError("card body is not valid JSON") from exc
        if isinstance(parsed, dict):
            return parsed, "json"
    raise TavernCardError(
        f"unrecognised card file (content-type {content_type or 'unknown'}); "
        "expected a card PNG, .charx, or JSON card"
    )
