"""Character-card & library assembly, shared by every provider.

Turns an extracted ``result`` dict (character + lorebook + meta) into Tavern V2/V3
cards embedded in a single self-contained PNG, and maintains the library index.
Provider-agnostic: JanitorAI, Saucepan and clank all funnel through
:func:`save_to_library`.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .lorebooks import update_lorebook_library
from .text import norm, normalize_user_placeholder, write_json

# Longest-side cap (px) for stored card portraits - keeps library PNGs small.
CARD_MAX_DIM = 512


def build_world_info(raw_entries: list[dict[str, Any]]) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    uid = 0
    for raw in raw_entries:
        content = str(raw.get("content") or raw.get("text") or "").strip()
        if not content:
            continue
        key = raw.get("key") or raw.get("keys") or raw.get("keywords") or []
        secondary = (
            raw.get("keysecondary")
            or raw.get("secondary_keys")
            or raw.get("keySecondary")
            or []
        )
        if isinstance(key, str):
            key = [x.strip() for x in key.split(",") if x.strip()]
        if isinstance(secondary, str):
            secondary = [x.strip() for x in secondary.split(",") if x.strip()]
        order = raw.get("order", raw.get("priority", raw.get("insertion_order", 100)))
        entries[str(uid)] = {
            "uid": uid,
            "key": key if isinstance(key, list) else [],
            "keysecondary": secondary if isinstance(secondary, list) else [],
            "comment": str(
                raw.get("comment")
                or raw.get("title")
                or raw.get("name")
                or f"Entry {uid}"
            ).strip(),
            "content": content,
            "constant": raw.get("constant") is True,
            "selective": raw.get("constant") is not True,
            "order": order if isinstance(order, (int, float)) else 100,
            "position": raw.get("position")
            if isinstance(raw.get("position"), int)
            else 0,
            "disable": raw.get("enabled") is False,
            "displayIndex": uid,
            "addMemo": True,
            "group": "",
            "groupOverride": False,
            "groupWeight": raw.get("groupWeight")
            if isinstance(raw.get("groupWeight"), int)
            else 100,
            "sticky": 0,
            "cooldown": 0,
            "delay": 0,
            "probability": 100,
            "depth": 4,
            "useProbability": True,
            "role": None,
            "vectorized": False,
            "excludeRecursion": False,
            "preventRecursion": False,
            "delayUntilRecursion": False,
            "scanDepth": None,
            "caseSensitive": None,
            "matchWholeWords": None,
            "useGroupScoring": None,
            "automationId": "",
            "selectiveLogic": raw.get("selectiveLogic")
            if isinstance(raw.get("selectiveLogic"), int)
            else 0,
            "ignoreBudget": False,
            "matchPersonaDescription": False,
            "matchCharacterDescription": False,
            "matchCharacterPersonality": False,
            "matchCharacterDepthPrompt": False,
            "matchScenario": False,
            "matchCreatorNotes": False,
            "outletName": "",
            "triggers": [],
            "characterFilter": {"isExclude": False, "names": [], "tags": []},
        }
        uid += 1
    return {"entries": entries}


def _public_book_entries(
    public_lorebooks: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Flatten accessible public-lorebook ``worldInfo`` entries (with real keys)."""
    out: list[dict[str, Any]] = []
    for book in public_lorebooks or []:
        entries = (book.get("worldInfo") or {}).get("entries") or {}
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            out.append(entry)
    return out


def build_character_book(
    entries: list[str] | None,
    public_lorebooks: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Assemble a Tavern ``character_book`` from all available lorebook data.

    Two sources, deduped by content: (1) accessible public-lorebook entries,
    which keep their real trigger ``keys``/``secondary_keys``/``comment``; and
    (2) the extracted closed-lorebook blocks, which are stored ``constant``
    (always active) with empty keys since we can't recover their trigger words.
    Includes the V3-required ``use_regex``/``extensions`` fields (harmless for V2).

    Returns ``None`` when there is no lorebook data at all, so the caller can omit
    the ``character_book`` field entirely rather than embedding an empty book.
    """
    book_entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(content: str, *, keys, secondary, comment, constant, enabled):
        text = normalize_user_placeholder(str(content or "").strip())
        if not text:
            return
        key = norm(text)
        if key in seen:
            return
        seen.add(key)
        order = len(book_entries)
        book_entries.append(
            {
                "id": order,
                "keys": keys if isinstance(keys, list) else [],
                "secondary_keys": secondary if isinstance(secondary, list) else [],
                "comment": str(comment or ""),
                "content": text,
                "constant": bool(constant),
                "selective": not bool(constant),
                "insertion_order": order,
                "enabled": bool(enabled),
                "position": "before_char",
                "case_sensitive": False,
                "name": str(comment or ""),
                "priority": 10,
                "use_regex": False,
                "extensions": {},
                "probability": 100,
            }
        )

    # 1) Public lorebook entries - preserve their real trigger keys.
    for entry in _public_book_entries(public_lorebooks):
        _add(
            entry.get("content"),
            keys=entry.get("key") or [],
            secondary=entry.get("keysecondary") or [],
            comment=entry.get("comment") or "",
            constant=entry.get("constant") is True,
            enabled=entry.get("disable") is not True,
        )
    # 2) Extracted closed-lorebook blocks - keyless, always active.
    for content in entries or []:
        _add(content, keys=[], secondary=[], comment="", constant=True, enabled=True)

    # No lorebook data at all → signal the caller to omit character_book entirely.
    if not book_entries:
        return None

    return {
        "name": "",
        "description": "",
        "scan_depth": 4,
        "token_budget": 2048,
        "recursive_scanning": False,
        "extensions": {},
        "entries": book_entries,
    }


def _card_provenance(
    meta: dict[str, Any] | None,
    source_url: str,
    character: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = meta or {}
    character = character or {}
    ripart: dict[str, Any] = {
        "ripper": "ripart-cli",
        "creator_id": meta.get("creator_id") or "",
        "source_url": source_url,
        "nsfw": bool(meta.get("is_nsfw")),
        # How the definition was obtained: "janitor" (public meta), "proxy"
        # (exact prompt echo), or "reconstructed-jllm" (lossy JanitorLLM leak).
        "definition_source": character.get("definitionSource") or "",
    }
    reconstruction = character.get("reconstruction")
    if isinstance(reconstruction, dict) and reconstruction:
        ripart["reconstruction"] = reconstruction
    return {"ripart": ripart}


def build_card_v2(
    character: dict[str, Any],
    entries: list[str] | None,
    *,
    meta: dict[str, Any] | None = None,
    public_lorebooks: list[dict[str, Any]] | None = None,
    source_url: str = "",
) -> dict[str, Any]:
    """Assemble a Tavern ``chara_card_v2`` dict with the lorebook embedded.

    The ``character_book`` field is included only when there is lorebook data;
    an empty book is omitted entirely.
    """
    meta = meta or {}
    data = {
        "name": character.get("name") or "",
        "description": character.get("description") or "",
        "personality": character.get("personality") or "",
        "scenario": character.get("scenario") or "",
        "first_mes": character.get("firstMessage") or "",
        "mes_example": character.get("exampleMessages") or "",
        "creator_notes": character.get("creatorNotes") or "",
        "system_prompt": "",
        "post_history_instructions": "",
        "tags": character.get("tags") or [],
        "creator": meta.get("creator_name") or "",
        "character_version": "1.0",
        "alternate_greetings": character.get("alternateGreetings") or [],
        "extensions": _card_provenance(meta, source_url, character),
    }
    book = build_character_book(entries, public_lorebooks)
    if book is not None:
        data["character_book"] = book
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def build_card_v3(
    character: dict[str, Any],
    entries: list[str] | None,
    *,
    meta: dict[str, Any] | None = None,
    public_lorebooks: list[dict[str, Any]] | None = None,
    source_url: str = "",
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Assemble a ``chara_card_v3`` dict (spec 3.0) with the lorebook embedded.

    Adds the V3-only fields over V2: ``group_only_greetings``, ``assets`` (an
    ``icon`` pointing at the card's own image via ``ccdefault:``), ``nickname``,
    ``creator_notes_multilingual``, ``source``, and creation/modification dates.
    """
    meta = meta or {}
    if timestamp is None:
        timestamp = int(datetime.now(timezone.utc).timestamp())
    data = {
        "name": character.get("name") or "",
        "description": character.get("description") or "",
        "personality": character.get("personality") or "",
        "scenario": character.get("scenario") or "",
        "first_mes": character.get("firstMessage") or "",
        "mes_example": character.get("exampleMessages") or "",
        "creator_notes": character.get("creatorNotes") or "",
        "system_prompt": "",
        "post_history_instructions": "",
        "tags": character.get("tags") or [],
        "creator": meta.get("creator_name") or "",
        "character_version": "1.0",
        "alternate_greetings": character.get("alternateGreetings") or [],
        "group_only_greetings": [],
        "nickname": "",
        "creator_notes_multilingual": {},
        "source": [source_url] if source_url else [],
        "assets": [{"type": "icon", "uri": "ccdefault:", "name": "main", "ext": "png"}],
        "creation_date": timestamp,
        "modification_date": timestamp,
        "extensions": _card_provenance(meta, source_url, character),
    }
    book = build_character_book(entries, public_lorebooks)
    if book is not None:
        data["character_book"] = book
    return {"spec": "chara_card_v3", "spec_version": "3.0", "data": data}


def encode_card_png(
    avatar: str | bytes | None, cards: dict[str, dict[str, Any]]
) -> bytes:
    """Return PNG bytes: the avatar with card metadata embedded in text chunks.

    ``cards`` maps chunk keyword -> card dict, e.g.
    ``{"ccv3": <v3 card>, "chara": <v2 card>}``. V3 readers prefer the ``ccv3``
    chunk; ``chara`` (base64 V2) stays for older readers. Falls back to a plain
    placeholder image when no avatar is available.
    """
    from io import BytesIO

    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    raw: bytes | None = None
    if isinstance(avatar, str) and avatar.startswith("data:image/"):
        try:
            raw = base64.b64decode(avatar.split(",", 1)[1])
        except Exception:
            raw = None
    elif isinstance(avatar, (bytes, bytearray)):
        raw = bytes(avatar)

    image = None
    if raw:
        try:
            image = Image.open(BytesIO(raw)).convert("RGB")
        except Exception:
            image = None
    if image is None:
        image = Image.new("RGB", (400, 600), (32, 34, 37))

    # Downscale oversized portraits so library cards stay small (avatars are
    # fetched at width=1200); never upscale.
    if max(image.size) > CARD_MAX_DIM:
        image.thumbnail((CARD_MAX_DIM, CARD_MAX_DIM), Image.LANCZOS)

    info = PngInfo()
    for keyword, card in cards.items():
        payload = base64.b64encode(
            json.dumps(card, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        info.add_text(keyword, payload)

    buffer = BytesIO()
    image.save(buffer, format="PNG", pnginfo=info, optimize=True)
    return buffer.getvalue()


def _update_library_index(
    library_dir: Path, character_id: str, result: dict[str, Any], entries: list[str]
) -> None:
    index_path = library_dir / "index.json"
    try:
        index = (
            json.loads(index_path.read_text(encoding="utf-8"))
            if index_path.exists()
            else {}
        )
        if not isinstance(index, dict):
            index = {}
    except Exception:
        index = {}
    meta = result.get("meta") or {}
    character = result.get("character") or {}
    index[character_id] = {
        "name": result.get("characterName") or character.get("name") or "",
        "creator": meta.get("creator_name") or "",
        "nsfw": bool(meta.get("is_nsfw")),
        "cardPublic": bool(meta.get("showdefinition")),
        "tags": character.get("tags") or [],
        "entryCount": len(entries),
        "lorebookChars": len(result.get("lorebookText") or ""),
        "definitionSource": character.get("definitionSource") or "",
        "url": result.get("url") or "",
        "extractedAt": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "file": f"{character_id}.png",
    }
    write_json(index_path, index)


def save_to_library(
    library_dir: Path, character_id: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Store one extracted character as a single self-contained card PNG.

    Layout: ``<library_dir>/<uuid>.png`` (V3 card + embedded lorebook) plus a
    shared ``<library_dir>/index.json`` acting as the lightweight database. The
    lorebook and card JSON both live inside the PNG, so no per-card folder.
    """
    character = result.get("character") or {}
    entries = result.get("entries") or []
    meta = result.get("meta") or {}
    public_lorebooks = result.get("publicLorebooks") or []
    source_url = result.get("url") or ""
    card_v3 = build_card_v3(
        character,
        entries,
        meta=meta,
        public_lorebooks=public_lorebooks,
        source_url=source_url,
    )
    card_v2 = build_card_v2(
        character,
        entries,
        meta=meta,
        public_lorebooks=public_lorebooks,
        source_url=source_url,
    )

    library_dir.mkdir(parents=True, exist_ok=True)
    png_path = library_dir / f"{character_id}.png"
    png_path.write_bytes(
        encode_card_png(
            character.get("avatarBase64"), {"ccv3": card_v3, "chara": card_v2}
        )
    )

    _update_library_index(library_dir, character_id, result, entries)
    lorebook_paths = update_lorebook_library(library_dir, character_id, result)

    paths = {"png": str(png_path)}
    if lorebook_paths:
        paths["lorebooks"] = lorebook_paths
    # Auto-publish to the Discord archive forum (UUID-keyed upsert). Best-effort
    # and env-gated: a no-op unless DISCORD_BOT_TOKEN is configured in .env, and
    # a Discord failure never propagates out of a rip. This is the single "push"
    # chokepoint every provider funnels through, so every rip publishes for free.
    from .discord_forum import publish_card

    published = publish_card(character_id, result, png_path)
    if published and published.get("thread_id"):
        paths["discord_thread"] = published["thread_id"]
    elif published and published.get("action") == "error":
        paths["discord_error"] = published.get("error") or "unknown Discord error"
    return paths
