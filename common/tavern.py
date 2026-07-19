"""Read *any* Tavern character card into RIPart's ``result`` shape.

This is the shared engine behind the "open site" providers (chub.ai,
character-tavern.com, …): those platforms don't gate anything — a character's
full definition ships as a **Character Card** (spec V1/V2/V3), either as a raw
JSON object from an API or embedded in a downloadable PNG. Every such site
funnels through here, so adding a new open platform is just "find the card, hand
it to :func:`card_to_result`".

It is the mirror image of :mod:`ripart.common.cards` (which *writes* cards):

* :func:`read_card_png` pulls the embedded card dict out of a card PNG's text
  chunks (``ccv3`` preferred, then ``chara``) — pure Pillow, no subprocess.
* :func:`card_to_result` normalises a V1/V2/V3 card dict into the ``result``
  dict every provider returns and :func:`ripart.common.cards.save_to_library`
  consumes, preserving lorebook trigger keys.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
from io import BytesIO
from typing import Any

def read_card_png(png_bytes: bytes) -> dict[str, Any] | None:
    """Return the embedded card dict from a Tavern card PNG, or ``None``.

    Reads the PNG's ``tEXt`` chunks and prefers the V3 ``ccv3`` chunk over the
    legacy base64 ``chara`` (V2) chunk. Values are base64-decoded then JSON
    parsed; a chunk that is already raw JSON is accepted too. Returns ``None``
    when the bytes aren't a PNG or carry no recognisable card chunk.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow is a hard dep
        raise RuntimeError("Pillow is required to read card PNGs") from exc

    try:
        image = Image.open(BytesIO(png_bytes))
    except Exception:
        return None
    chunks = getattr(image, "text", {}) or {}
    for keyword in ("ccv3", "chara"):
        raw = chunks.get(keyword)
        if not raw:
            continue
        card = _decode_card_chunk(raw)
        if card is not None:
            return card
    return None


def _decode_card_chunk(raw: str) -> dict[str, Any] | None:
    """Decode a PNG card chunk that is base64-JSON (usual) or bare JSON."""
    for candidate in (_maybe_b64(raw), raw):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _maybe_b64(raw: str) -> str | None:
    try:
        return base64.b64decode(raw).decode("utf-8", "ignore")
    except (binascii.Error, ValueError):
        return None


def _card_data(card: dict[str, Any]) -> dict[str, Any]:
    """The card body: the ``data`` object for V2/V3, else the card itself (V1)."""
    data = card.get("data")
    return data if isinstance(data, dict) else card


def _as_text(value: Any) -> str:
    """Coerce a card field (str, or list of turns/lines) to trimmed text."""
    if isinstance(value, list):
        return "\n\n".join(str(v).strip() for v in value if str(v).strip()).strip()
    return str(value or "").strip()


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def lorebook_to_public(book: Any) -> dict[str, Any] | None:
    """Convert a Tavern ``character_book`` into a RIPart ``publicLorebooks`` entry.

    RIPart's :func:`ripart.common.cards.build_character_book` reads public
    lorebooks in JanitorAI's ``worldInfo`` shape (so it keeps real trigger
    keys). A Tavern card's ``character_book`` uses ``keys``/``secondary_keys``/
    ``enabled``; this remaps each entry to that ``worldInfo`` shape. Accepts the
    ``entries`` as either a list or a ``{uid: entry}`` dict. Returns ``None``
    when there are no usable entries, so callers can skip it entirely.
    """
    if not isinstance(book, dict):
        return None
    raw_entries = book.get("entries")
    if isinstance(raw_entries, dict):
        raw_entries = list(raw_entries.values())
    if not isinstance(raw_entries, list):
        return None

    entries: dict[str, Any] = {}
    uid = 0
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        extensions = entry.get("extensions")
        ripart = extensions.get("ripart") if isinstance(extensions, dict) else None
        original = ripart.get("sillytavern") if isinstance(ripart, dict) else None
        restored = copy.deepcopy(original) if isinstance(original, dict) else {}
        if isinstance(extensions, dict):
            extension_to_st = {
                "display_index": "displayIndex",
                "exclude_recursion": "excludeRecursion",
                "prevent_recursion": "preventRecursion",
                "delay_until_recursion": "delayUntilRecursion",
                "depth": "depth",
                "probability": "probability",
                "position": "position",
                "role": "role",
                "outlet_name": "outletName",
                "group": "group",
                "group_override": "groupOverride",
                "group_weight": "groupWeight",
                "scan_depth": "scanDepth",
                "case_sensitive": "caseSensitive",
                "match_whole_words": "matchWholeWords",
                "use_group_scoring": "useGroupScoring",
                "automation_id": "automationId",
                "vectorized": "vectorized",
                "sticky": "sticky",
                "cooldown": "cooldown",
                "delay": "delay",
                "match_persona_description": "matchPersonaDescription",
                "match_character_description": "matchCharacterDescription",
                "match_character_personality": "matchCharacterPersonality",
                "match_character_depth_prompt": "matchCharacterDepthPrompt",
                "match_scenario": "matchScenario",
                "match_creator_notes": "matchCreatorNotes",
                "triggers": "triggers",
                "ignore_budget": "ignoreBudget",
                "useProbability": "useProbability",
                "selectiveLogic": "selectiveLogic",
            }
            for extension_name, st_name in extension_to_st.items():
                if extension_name in extensions and st_name not in restored:
                    restored[st_name] = copy.deepcopy(extensions[extension_name])
        entry_id = entry.get("id", uid)
        card_position = entry.get("position", "before_char")
        original_position = restored.get("position")
        if original_position in (0, 1):
            restored_position: Any = 0 if card_position == "before_char" else 1
        elif original_position not in (None, "before_char", "after_char"):
            # Character Card V3 cannot represent ST's depth/A.N./outlet/example
            # positions. Retain the exact value carried in our extension.
            restored_position = original_position
        else:
            restored_position = card_position
        restored.update({
            "uid": entry_id,
            "key": _str_list(entry.get("keys") or entry.get("key")),
            "keysecondary": _str_list(
                entry.get("secondary_keys") or entry.get("keysecondary")
            ),
            "content": content,
            "comment": str(entry.get("comment") or entry.get("name") or "").strip(),
            "constant": entry.get("constant") is True,
            "selective": entry.get("selective") is True,
            # JanitorAI marks a *disabled* entry; Tavern marks an *enabled* one.
            "disable": entry.get("enabled") is False,
            "order": entry.get("insertion_order", uid),
            "position": restored_position,
            "caseSensitive": entry.get("case_sensitive")
            if entry.get("case_sensitive") is not None
            else restored.get("caseSensitive"),
            "useRegex": entry.get("use_regex") is True,
            "extensions": copy.deepcopy(extensions) if isinstance(extensions, dict) else {},
        })
        if isinstance(entry.get("priority"), (int, float)) and not isinstance(
            entry.get("priority"), bool
        ):
            restored["priority"] = entry["priority"]
        entries[str(uid)] = restored
        uid += 1
    if not entries:
        return None
    return {
        "title": str(book.get("name") or ""),
        "name": str(book.get("name") or ""),
        "description": str(book.get("description") or ""),
        "scan_depth": book.get("scan_depth"),
        "token_budget": book.get("token_budget"),
        "recursive_scanning": book.get("recursive_scanning"),
        "extensions": copy.deepcopy(book.get("extensions"))
        if isinstance(book.get("extensions"), dict)
        else {},
        "worldInfo": {"entries": entries},
    }


def card_to_result(
    card: dict[str, Any],
    *,
    source_url: str,
    character_id: str,
    definition_source: str,
    character_name: str | None = None,
    avatar_base64: str = "",
    creator_name: str = "",
    creator_id: str = "",
    is_nsfw: bool = False,
    extra_notes: str = "",
    extra_meta: dict[str, Any] | None = None,
    extra_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalise a V1/V2/V3 Tavern card dict into a RIPart ``result`` dict.

    The returned dict is shaped for
    :func:`ripart.common.cards.save_to_library`: the card body maps onto
    ``character`` and the embedded ``character_book`` onto ``publicLorebooks``
    (keeping real trigger keys and entry extensions). Prompt overrides and V3-only
    fields are retained on ``character`` so saving the result does not change
    their meaning. ``definition_source`` labels where the card came from (e.g.
    ``"chub-api"``, ``"tavern-png"``).
    """
    data = _card_data(card)
    name = (character_name or _as_text(data.get("name")) or "Unknown").strip()

    note_parts: list[str] = []
    if extra_notes.strip():
        note_parts.append(extra_notes.strip())
    creator_notes = _as_text(data.get("creator_notes"))
    if creator_notes:
        note_parts.append(creator_notes)
    public_book = lorebook_to_public(data.get("character_book"))
    public_lorebooks = [public_book] if public_book else []
    entry_count = len((public_book or {}).get("worldInfo", {}).get("entries", {}))

    character = {
        "name": name,
        "avatarBase64": avatar_base64,
        "description": _as_text(data.get("description")),
        "personality": _as_text(data.get("personality")),
        "scenario": _as_text(data.get("scenario")),
        "firstMessage": _as_text(data.get("first_mes") or data.get("first_message")),
        "alternateGreetings": _str_list(data.get("alternate_greetings")),
        "exampleMessages": _as_text(
            data.get("mes_example") or data.get("example_dialogs")
        ),
        "creatorNotes": "\n\n".join(note_parts),
        "tags": _str_list(data.get("tags")),
        "systemPrompt": _as_text(data.get("system_prompt")),
        "postHistoryInstructions": _as_text(data.get("post_history_instructions")),
        "creator": _as_text(data.get("creator")),
        "characterVersion": _as_text(data.get("character_version")),
        "groupOnlyGreetings": _str_list(data.get("group_only_greetings")),
        "nickname": _as_text(data.get("nickname")),
        "creatorNotesMultilingual": copy.deepcopy(data.get("creator_notes_multilingual"))
        if isinstance(data.get("creator_notes_multilingual"), dict)
        else {},
        "source": _str_list(data.get("source")),
        "assets": copy.deepcopy(data.get("assets"))
        if isinstance(data.get("assets"), list)
        else [],
        "creationDate": data.get("creation_date"),
        "modificationDate": data.get("modification_date"),
        "extensions": copy.deepcopy(data.get("extensions"))
        if isinstance(data.get("extensions"), dict)
        else {},
        "definitionSource": definition_source,
    }

    meta = {
        "name": name,
        "creator_name": (creator_name or _as_text(data.get("creator"))).strip(),
        "creator_id": creator_id,
        "is_nsfw": bool(is_nsfw),
        # These cards are public by definition — the whole point of "open" sites.
        "showdefinition": True,
    }
    if extra_meta:
        meta.update(extra_meta)

    diagnostics = {
        "characterId": character_id,
        "definitionSource": definition_source,
        "descriptionChars": len(character["description"]),
        "personalityChars": len(character["personality"]),
        "scenarioChars": len(character["scenario"]),
        "firstMessageChars": len(character["firstMessage"]),
        "exampleChars": len(character["exampleMessages"]),
        "alternateGreetings": len(character["alternateGreetings"]),
        "tags": character["tags"],
        "lorebookEntries": entry_count,
        "spec": card.get("spec") or "chara_card_v1",
    }
    if extra_diagnostics:
        diagnostics.update(extra_diagnostics)

    return {
        "url": source_url,
        "characterId": character_id,
        "characterName": name,
        "character": character,
        "meta": meta,
        "publicLorebooks": public_lorebooks,
        "entries": [],
        "lorebookText": "",
        "leakRaw": "",
        "diagnostics": diagnostics,
    }
