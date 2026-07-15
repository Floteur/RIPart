"""Saucepan (saucepan.ai) native extraction.

Unlike the JanitorAI path, Saucepan needs no browser: its companion definition
is available directly from the authenticated REST API. The catch is that
definitions ship as a SHUFFLED list of text fragments padded with decoy
fragments — a naive join is garbled. Each real fragment carries a ``proof`` hash
that the decoys fail; reassembly validates the proof, orders the survivors by
``key ^ mask``, and concatenates. Ported from Saucepan's own web client so the
output matches byte-for-byte.

Data comes from two endpoints:
  GET /api/v1/companion/definition?companion_id=ID  -> named prose sections
      (Companion Core, Example Dialogue, Advanced Prompt, Response Formatting)
  GET /api/v2/companions/ID                          -> metadata + the body
      fragments + starting scenarios (the greetings; absent from definition)

The bearer token is persisted (via ``login``/``set_token``) to a gitignored file
under the project root and reused by every command, mirroring the JanitorAI
browser profile.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

import requests

SAUCEPAN_BASE = "https://saucepan.ai"
SAUCEPAN_ORIGIN = "https://saucepan.ai"
SAUCEPAN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
MAX_IMAGE_BYTES = 12 * 1024 * 1024
TIMEOUT = 30

# Token lives next to the code, gitignored (see .gitignore). One line, the raw
# bearer token; never printed back to the user.
TOKEN_FILE = Path(__file__).resolve().parent / ".saucepan-token"


class SaucepanError(Exception):
    """User-facing Saucepan failure; carries an optional HTTP-ish status code."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Persisted bearer token
# --------------------------------------------------------------------------- #

_token = ""


def load_token() -> str:
    """Read the persisted token into memory (called on first use)."""
    global _token
    if not _token and TOKEN_FILE.exists():
        _token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    return _token


def set_token(value: str) -> None:
    """Store a token both in memory and on disk."""
    global _token
    _token = str(value or "").strip()
    if _token:
        TOKEN_FILE.write_text(_token, encoding="utf-8")


def clear_token() -> None:
    """Forget the token (log out)."""
    global _token
    _token = ""
    TOKEN_FILE.unlink(missing_ok=True)


def has_token() -> bool:
    return bool(load_token())


def _headers(with_auth: bool = False, referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": SAUCEPAN_UA,
        "Accept": "*/*",
        # requests auto-decompresses gzip/deflate/br (brotli is installed); we
        # never negotiate zstd so there is nothing left to hand-decode.
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": SAUCEPAN_ORIGIN,
        "Referer": referer or f"{SAUCEPAN_ORIGIN}/",
        "x-saucepan-client-version": "1",
    }
    if with_auth and load_token():
        headers["Authorization"] = f"Bearer {_token}"
    return headers


# --------------------------------------------------------------------------- #
# Fragment reassembly (verbatim port of Saucepan's client scheme)
# --------------------------------------------------------------------------- #

_FNV_OFFSET = 2166136261
_FNV_PRIME = 16777619
_U32 = 0xFFFFFFFF


def _rotl(value: int, bits: int) -> int:
    value &= _U32
    return ((value << bits) | (value >> (32 - bits))) & _U32


def _fragment_hash(mask: int, derived_key: int, text: str) -> int:
    h = (_FNV_OFFSET ^ _rotl(mask, 7) ^ _rotl(derived_key, 13)) & _U32
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * _FNV_PRIME) & _U32
    return h & _U32


def assemble_fragments(content: dict[str, Any] | None) -> str:
    """Reassemble a ``{fragments, mask}`` content object, dropping decoys."""
    content = content or {}
    fragments = content.get("fragments")
    if not isinstance(fragments, list):
        return ""
    mask = int(content.get("mask") or 0) & _U32

    survivors: list[dict[str, Any]] = []
    for frag in fragments:
        if not isinstance(frag, dict) or not isinstance(frag.get("text"), str):
            continue
        derived_key = (int(frag.get("key") or 0) ^ mask) & _U32
        if _fragment_hash(mask, derived_key, frag["text"]) == (int(frag.get("proof") or 0) & _U32):
            survivors.append(frag)

    survivors.sort(key=lambda f: (int(f.get("key") or 0) ^ mask) & _U32)
    return "".join(f["text"] for f in survivors)


# --------------------------------------------------------------------------- #
# API helpers
# --------------------------------------------------------------------------- #


def _get_json(path: str, with_auth: bool) -> tuple[bool, int, Any]:
    try:
        response = requests.get(
            f"{SAUCEPAN_BASE}{path}", headers=_headers(with_auth), timeout=TIMEOUT
        )
    except requests.RequestException as exc:
        raise SaucepanError(f"network error talking to Saucepan: {exc}") from exc
    data: Any = None
    try:
        data = response.json()
    except ValueError:
        data = None
    return response.ok, response.status_code, data


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


def login(handle: str, password: str) -> str:
    """Log in with handle + password; store and return the bearer token."""
    try:
        response = requests.post(
            f"{SAUCEPAN_BASE}/api/v1/auth/sign_in_password",
            headers={
                **_headers(with_auth=False, referer=f"{SAUCEPAN_ORIGIN}/sign-in"),
                "Content-Type": "application/json",
            },
            json={"handle": str(handle or "").strip(), "password": str(password or "")},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SaucepanError(f"network error talking to Saucepan: {exc}") from exc

    try:
        data = response.json()
    except ValueError:
        data = {}
    if not response.ok:
        message = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
        raise SaucepanError(message or f"Saucepan HTTP {response.status_code}", response.status_code)

    token = None
    if isinstance(data, dict):
        token = data.get("token") or data.get("access_token") or data.get("session_token") or data.get("sessionToken")
    if not token:
        raise SaucepanError("login succeeded but no token was returned")
    set_token(token)
    return token


def fetch_avatar(image_id: str | None) -> str:
    """Download the companion avatar as a ``data:`` URI (or '' on failure)."""
    if not image_id:
        return ""
    try:
        response = requests.get(
            f"{SAUCEPAN_BASE}/cdn/{requests.utils.quote(str(image_id), safe='')}/card",
            headers={
                "User-Agent": SAUCEPAN_UA,
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                "Referer": f"{SAUCEPAN_ORIGIN}/",
            },
            timeout=TIMEOUT,
        )
        if not response.ok:
            return ""
        content = response.content
        if len(content) > MAX_IMAGE_BYTES:
            return ""
        content_type = response.headers.get("content-type") or "image/jpeg"
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"
    except requests.RequestException:
        return ""


def _companion_creator(companion: dict[str, Any]) -> tuple[str, str]:
    """Best-effort (creator_name, creator_id) from a companion payload."""
    for key in ("creator", "user", "owner", "author"):
        holder = companion.get(key)
        if isinstance(holder, dict):
            name = holder.get("display_name") or holder.get("handle") or holder.get("name") or ""
            return str(name).strip(), str(holder.get("id") or "")
    return "", ""


def extract_companion(url: str) -> dict[str, Any]:
    """Fetch a Saucepan companion by URL and build a RIPart ``result`` dict.

    The returned dict is shaped for ``helpers.save_to_library``: it carries the
    ``character`` object plus ``meta``/``entries``/``publicLorebooks``/``url``.
    Requires a stored bearer token (definition + scenarios are auth-gated).
    """
    if not has_token():
        raise SaucepanError("no Saucepan token configured — run `rip saucepan login` first", 401)
    companion_id = parse_companion_id(url)
    if not companion_id:
        raise SaucepanError("not a Saucepan companion URL", 400)

    def_ok, def_status, def_data = _get_json(
        f"/api/v1/companion/definition?companion_id={requests.utils.quote(companion_id, safe='')}", True
    )
    comp_ok, comp_status, comp_data = _get_json(
        f"/api/v2/companions/{requests.utils.quote(companion_id, safe='')}", True
    )

    if not def_ok:
        message = None
        if isinstance(def_data, dict):
            message = (def_data.get("error") or {}).get("message")
        raise SaucepanError(message or f"Saucepan HTTP {def_status}", 401 if def_status == 401 else 502)

    # Named prose sections from the definition endpoint.
    sections: dict[str, str] = {}
    for section in (def_data.get("sections") if isinstance(def_data, dict) else None) or []:
        if isinstance(section, dict) and section.get("title") and section.get("content"):
            sections[section["title"]] = assemble_fragments(section["content"])

    companion = comp_data.get("companion") if isinstance(comp_data, dict) else None
    if not comp_ok or not isinstance(companion, dict):
        # Non-fatal: the definition endpoint already carries the body + prose;
        # only greetings/metadata come from here.
        companion = companion if isinstance(companion, dict) else {}

    # Body: prefer the definition's "Companion Core", fall back to the v2 body.
    description = sections.get("Companion Core") or ""
    if not description and companion.get("full_description_fragments"):
        description = assemble_fragments(companion["full_description_fragments"])

    # Greetings live only on the v2 companion as starting scenarios.
    greetings: list[str] = []
    for scenario in companion.get("starting_scenarios_fragments") or []:
        text = assemble_fragments(scenario.get("message") if isinstance(scenario, dict) else None)
        if text and text.strip():
            greetings.append(text)

    # Advanced Prompt / Response Formatting have no dedicated card field; keep
    # them (labeled) in creator notes so nothing authored is silently dropped.
    notes_parts: list[str] = []
    short_desc = str(companion.get("short_description") or "").strip()
    if short_desc:
        notes_parts.append(short_desc)
    if sections.get("Advanced Prompt"):
        notes_parts.append(f"--- Advanced Prompt ---\n{sections['Advanced Prompt']}")
    if sections.get("Response Formatting Instructions"):
        notes_parts.append(f"--- Response Formatting ---\n{sections['Response Formatting Instructions']}")

    image = companion.get("image")
    image_id = image.get("id") if isinstance(image, dict) else None
    avatar_base64 = fetch_avatar(image_id)

    name = companion.get("display_name") or companion.get("name") or "Unknown"
    tags = companion.get("tags") if isinstance(companion.get("tags"), list) else []
    creator_name, creator_id = _companion_creator(companion)
    is_nsfw = bool(companion.get("is_nsfw") or companion.get("nsfw"))

    character = {
        "name": name,
        "avatarBase64": avatar_base64,
        "description": description,
        "personality": "",
        "scenario": "",
        "firstMessage": greetings[0] if greetings else "",
        "alternateGreetings": greetings[1:],
        "exampleMessages": sections.get("Example Dialogue") or "",
        "creatorNotes": "\n\n".join(notes_parts),
        "tags": tags,
        "definitionSource": "saucepan",
    }

    meta = {
        "name": name,
        "creator_name": creator_name,
        "creator_id": creator_id,
        "is_nsfw": is_nsfw,
        # Saucepan's definition is an exact pull, so treat it as a public card.
        "showdefinition": True,
    }

    return {
        "url": url if url.startswith(("http://", "https://")) else f"{SAUCEPAN_BASE}/companion/{companion_id}",
        "characterId": companion_id,
        "characterName": name,
        "character": character,
        "meta": meta,
        "publicLorebooks": [],
        "entries": [],
        "lorebookText": "",
        "diagnostics": {"greetings": len(greetings), "sections": sorted(sections.keys())},
    }
