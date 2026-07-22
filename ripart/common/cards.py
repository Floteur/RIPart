"""Character-card & library assembly, shared by every provider.

Turns an extracted ``result`` dict (character + lorebook + meta) into Tavern V2/V3
cards embedded in a single self-contained PNG, and maintains the library index.
Provider-agnostic: JanitorAI, Saucepan and clank all funnel through
:func:`save_to_library`.
"""

from __future__ import annotations

import base64
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .lorebooks import update_lorebook_library
from .text import norm, normalize_user_placeholder, write_json

# Discord accepts up to 10 MB per attachment; keep full-res portraits under it.
CARD_MAX_BYTES = 10 * 1024 * 1024


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
        # Start with the provider payload so newer SillyTavern options survive
        # even when RIPart does not understand them yet.  The normalized fields
        # below make the entry usable by the rest of RIPart without replacing
        # explicitly authored values with our defaults.
        entry = copy.deepcopy(raw)
        entry.update(
            {
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
                "selective": raw.get("selective")
                if isinstance(raw.get("selective"), bool)
                else bool(secondary),
                "order": order if isinstance(order, (int, float)) else 100,
                "position": raw.get("position", 0),
                "disable": raw.get("disable") is True or raw.get("enabled") is False,
                "displayIndex": raw.get("displayIndex", uid),
                "addMemo": raw.get("addMemo", True),
                "group": raw.get("group", ""),
                "groupOverride": raw.get("groupOverride", False),
                "groupWeight": raw.get("groupWeight")
                if isinstance(raw.get("groupWeight"), int)
                else 100,
                "sticky": raw.get("sticky", 0),
                "cooldown": raw.get("cooldown", 0),
                "delay": raw.get("delay", 0),
                "probability": raw.get("probability", 100),
                "depth": raw.get("depth", 4),
                "useProbability": raw.get("useProbability", True),
                "role": raw.get("role"),
                "vectorized": raw.get("vectorized", False),
                "excludeRecursion": raw.get("excludeRecursion", False),
                "preventRecursion": raw.get("preventRecursion", False),
                "delayUntilRecursion": raw.get("delayUntilRecursion", False),
                "scanDepth": raw.get("scanDepth"),
                "caseSensitive": raw.get("caseSensitive", raw.get("case_sensitive")),
                "matchWholeWords": raw.get("matchWholeWords"),
                "useGroupScoring": raw.get("useGroupScoring"),
                "automationId": raw.get("automationId", ""),
                "selectiveLogic": raw.get("selectiveLogic")
                if isinstance(raw.get("selectiveLogic"), int)
                else 0,
                "ignoreBudget": raw.get("ignoreBudget", False),
                "matchPersonaDescription": raw.get("matchPersonaDescription", False),
                "matchCharacterDescription": raw.get(
                    "matchCharacterDescription", False
                ),
                "matchCharacterPersonality": raw.get(
                    "matchCharacterPersonality", False
                ),
                "matchCharacterDepthPrompt": raw.get(
                    "matchCharacterDepthPrompt", False
                ),
                "matchScenario": raw.get("matchScenario", False),
                "matchCreatorNotes": raw.get("matchCreatorNotes", False),
                "outletName": raw.get("outletName", ""),
                "triggers": copy.deepcopy(raw.get("triggers", [])),
                "characterFilter": copy.deepcopy(
                    raw.get(
                        "characterFilter", {"isExclude": False, "names": [], "tags": []}
                    )
                ),
            }
        )
        entries[str(uid)] = entry
        uid += 1
    return {"entries": entries}


def _public_book_entries(
    public_lorebooks: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Flatten accessible public-lorebook ``worldInfo`` entries (with real keys)."""
    out: list[dict[str, Any]] = []
    for book in public_lorebooks or []:
        if not isinstance(book, dict):
            continue
        world_info = book.get("worldInfo")
        entries = world_info.get("entries") if isinstance(world_info, dict) else None
        values = entries.values() if isinstance(entries, dict) else entries
        if not isinstance(values, (list, tuple)) and not isinstance(entries, dict):
            continue
        for entry in values:
            if not isinstance(entry, dict):
                continue
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            out.append(entry)
    return out


def _first_value(entry: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in entry and entry[name] is not None:
            return entry[name]
    return default


def _entry_extensions(entry: dict[str, Any]) -> dict[str, Any]:
    extensions = copy.deepcopy(entry.get("extensions"))
    if not isinstance(extensions, dict):
        extensions = {}
    ripart = extensions.get("ripart")
    if not isinstance(ripart, dict):
        ripart = {}
    # These are SillyTavern's documented Character Book extension keys. They
    # are consumed by ST itself when the card is imported, so the behavior is
    # preserved even outside RIPart rather than merely archived for round-trip.
    st_extension_fields = {
        "displayIndex": "display_index",
        "excludeRecursion": "exclude_recursion",
        "preventRecursion": "prevent_recursion",
        "delayUntilRecursion": "delay_until_recursion",
        "depth": "depth",
        "probability": "probability",
        "position": "position",
        "role": "role",
        "outletName": "outlet_name",
        "group": "group",
        "groupOverride": "group_override",
        "groupWeight": "group_weight",
        "scanDepth": "scan_depth",
        "caseSensitive": "case_sensitive",
        "matchWholeWords": "match_whole_words",
        "useGroupScoring": "use_group_scoring",
        "automationId": "automation_id",
        "vectorized": "vectorized",
        "sticky": "sticky",
        "cooldown": "cooldown",
        "delay": "delay",
        "matchPersonaDescription": "match_persona_description",
        "matchCharacterDescription": "match_character_description",
        "matchCharacterPersonality": "match_character_personality",
        "matchCharacterDepthPrompt": "match_character_depth_prompt",
        "matchScenario": "match_scenario",
        "matchCreatorNotes": "match_creator_notes",
        "triggers": "triggers",
        "ignoreBudget": "ignore_budget",
        # SillyTavern intentionally uses these camelCase extension keys.
        "useProbability": "useProbability",
        "selectiveLogic": "selectiveLogic",
    }
    for source_name, extension_name in st_extension_fields.items():
        if source_name in entry:
            extensions[extension_name] = copy.deepcopy(entry[source_name])
    source_entry = copy.deepcopy(entry)
    source_extensions = source_entry.get("extensions")
    if isinstance(source_extensions, dict):
        source_ripart = source_extensions.get("ripart")
        if isinstance(source_ripart, dict):
            source_ripart.pop("sillytavern", None)
            if not source_ripart:
                source_extensions.pop("ripart", None)
        if not source_extensions:
            source_entry.pop("extensions", None)
    # Keeping the exact source object under extensions gives RIPart a lossless
    # round trip for SillyTavern-only options that Character Card V3 cannot
    # express directly (timed effects, filters, outlets, vectorization, etc.).
    ripart["sillytavern"] = source_entry
    extensions["ripart"] = ripart
    return extensions


def build_character_book(
    entries: list[str] | None,
    public_lorebooks: list[dict[str, Any]] | None = None,
    recovered_triggers: dict[str, list[str]] | None = None,
    recovered_constants: list[str] | set[str] | None = None,
) -> dict[str, Any] | None:
    """Assemble a Tavern ``character_book`` from all available lorebook data.

    Accessible public-lorebook entries retain their standard Character Card
    fields and carry exact SillyTavern-only settings inside ``extensions``.
    Extracted closed-lorebook blocks have unknown triggers, so they are embedded
    disabled for safe manual review rather than changed into always-active lore.

    Returns ``None`` when there is no lorebook data at all, so the caller can omit
    the ``character_book`` field entirely rather than embedding an empty book.
    """
    book_entries: list[dict[str, Any]] = []
    public_content: set[str] = set()
    recovered_seen: set[str] = set()
    recovered_constant_keys = set(recovered_constants or [])

    def _add_public(entry: dict[str, Any]) -> None:
        content = entry.get("content")
        text = normalize_user_placeholder(str(content or "").strip())
        if not text:
            return
        public_content.add(norm(text))
        keys = _first_value(entry, "keys", "key", default=[])
        secondary = _first_value(entry, "secondary_keys", "keysecondary", default=[])
        if isinstance(keys, str):
            keys = [part.strip() for part in keys.split(",") if part.strip()]
        if isinstance(secondary, str):
            secondary = [part.strip() for part in secondary.split(",") if part.strip()]
        constant = entry.get("constant") is True
        position = _first_value(entry, "position", default="before_char")
        if position == 0:
            position = "before_char"
        elif position == 1:
            position = "after_char"
        if position not in ("before_char", "after_char"):
            position = "before_char"
        comment = str(entry.get("comment") or entry.get("name") or "")
        converted: dict[str, Any] = {
            "id": _first_value(entry, "id", "uid", default=len(book_entries)),
            "keys": keys if isinstance(keys, list) else [],
            "secondary_keys": secondary if isinstance(secondary, list) else [],
            "comment": comment,
            "content": text,
            "constant": constant,
            "selective": entry.get("selective")
            if isinstance(entry.get("selective"), bool)
            else bool(secondary),
            "insertion_order": _first_value(
                entry, "insertion_order", "order", default=len(book_entries)
            ),
            "enabled": not (
                entry.get("disable") is True or entry.get("enabled") is False
            ),
            "position": position,
            "case_sensitive": bool(
                _first_value(entry, "case_sensitive", "caseSensitive", default=False)
            ),
            "name": str(entry.get("name") or comment),
            "use_regex": bool(
                _first_value(entry, "use_regex", "useRegex", default=True)
            ),
            "extensions": _entry_extensions(entry),
        }
        priority = entry.get("priority")
        if isinstance(priority, (int, float)) and not isinstance(priority, bool):
            converted["priority"] = priority
        book_entries.append(converted)

    def _add_recovered(content: str) -> None:
        text = normalize_user_placeholder(str(content or "").strip())
        key = norm(text)
        if not key or key in public_content or key in recovered_seen:
            return
        recovered_seen.add(key)
        order = len(book_entries)
        triggers = (
            list(dict.fromkeys(recovered_triggers.get(key, [])))
            if recovered_triggers
            else []
        )
        constant = key in recovered_constant_keys
        book_entries.append(
            {
                "id": f"recovered-{order}",
                "keys": triggers,
                "secondary_keys": [],
                "comment": (
                    "Recovered lore; always active in probe"
                    if constant
                    else "Recovered lore; activation inferred by RIPart probe"
                    if triggers
                    else "Recovered lore; original activation is unknown"
                ),
                "content": text,
                "constant": constant,
                "selective": False,
                "insertion_order": order,
                # Inferred keys are verified by a fresh one-key generation;
                # unknown entries remain disabled for safe manual review.
                "enabled": bool(triggers or constant),
                "position": "before_char",
                "case_sensitive": False,
                "name": (
                    "Recovered lore"
                    if (triggers or constant)
                    else "Recovered lore (disabled)"
                ),
                "use_regex": False,
                "extensions": {
                    "ripart": {
                        "recovery": {
                            "trigger_status": (
                                "constant"
                                if constant
                                else "inferred"
                                if triggers
                                else "unknown"
                            ),
                            "inferred_keys": triggers,
                            **(
                                {}
                                if triggers or constant
                                else {
                                    "reason_disabled": "original activation conditions unavailable"
                                }
                            ),
                        }
                    }
                },
            }
        )

    # 1) Public lorebook entries - preserve their real trigger keys.
    for entry in _public_book_entries(public_lorebooks):
        _add_public(entry)
    # 2) Extracted closed-lorebook blocks have unknown activation conditions.
    for content in entries or []:
        _add_recovered(content)

    # No lorebook data at all → signal the caller to omit character_book entirely.
    if not book_entries:
        return None

    source_books = [book for book in public_lorebooks or [] if isinstance(book, dict)]
    primary = source_books[0] if len(source_books) == 1 else {}
    extensions = copy.deepcopy(primary.get("extensions"))
    if not isinstance(extensions, dict):
        extensions = {}
    if len(source_books) > 1:
        ripart = extensions.get("ripart")
        if not isinstance(ripart, dict):
            ripart = {}
        ripart["source_books"] = copy.deepcopy(source_books)
        extensions["ripart"] = ripart
    book: dict[str, Any] = {
        "name": str(primary.get("name") or primary.get("title") or ""),
        "description": str(primary.get("description") or ""),
        "extensions": extensions,
        "entries": book_entries,
    }
    scan_depth = _first_value(primary, "scan_depth", "scanDepth")
    if isinstance(scan_depth, (int, float)) and not isinstance(scan_depth, bool):
        book["scan_depth"] = scan_depth
    token_budget = _first_value(primary, "token_budget", "tokenBudget")
    if isinstance(token_budget, (int, float)) and not isinstance(token_budget, bool):
        book["token_budget"] = token_budget
    recursive = _first_value(primary, "recursive_scanning", "recursiveScanning")
    if isinstance(recursive, bool):
        book["recursive_scanning"] = recursive
    return book


def _card_provenance(
    meta: dict[str, Any] | None,
    source_url: str,
    character: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = meta or {}
    character = character or {}
    from .. import __version__ as ripart_version  # deferred: avoids import cycle

    source = character.get("definitionSource") or ""
    ripart: dict[str, Any] = {
        "ripper": "ripart-cli",
        # Which ripper build + method produced this dump, e.g.
        # "ripart-cli/0.1.0 proxy" - lets a re-rip be compared against its source.
        "dump_version": f"ripart-cli/{ripart_version}{' ' + source if source else ''}",
        "creator_id": meta.get("creator_id") or "",
        "source_url": source_url,
        "nsfw": bool(meta.get("is_nsfw")),
        # How the definition was obtained: "janitor" (public meta), "proxy"
        # (exact prompt echo), or "reconstructed-jllm" (lossy JanitorLLM leak).
        "definition_source": source,
    }
    reconstruction = character.get("reconstruction")
    if isinstance(reconstruction, dict) and reconstruction:
        ripart["reconstruction"] = reconstruction
    extensions = copy.deepcopy(character.get("extensions"))
    if not isinstance(extensions, dict):
        extensions = {}
    prior_ripart = extensions.get("ripart")
    if isinstance(prior_ripart, dict):
        ripart = {**prior_ripart, **ripart}
    extensions["ripart"] = ripart
    return extensions


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _card_sources(character: dict[str, Any], source_url: str) -> list[str]:
    sources = _string_list(character.get("source"))
    if source_url and source_url not in sources:
        sources.append(source_url)
    return sources


def build_card_v2(
    character: dict[str, Any],
    entries: list[str] | None,
    *,
    meta: dict[str, Any] | None = None,
    public_lorebooks: list[dict[str, Any]] | None = None,
    recovered_triggers: dict[str, list[str]] | None = None,
    recovered_constants: list[str] | set[str] | None = None,
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
        "system_prompt": character.get("systemPrompt") or "",
        "post_history_instructions": character.get("postHistoryInstructions") or "",
        "tags": character.get("tags") or [],
        "creator": meta.get("creator_name") or character.get("creator") or "",
        "character_version": character.get("characterVersion") or "1.0",
        "alternate_greetings": character.get("alternateGreetings") or [],
        "extensions": _card_provenance(meta, source_url, character),
    }
    book = build_character_book(
        entries, public_lorebooks, recovered_triggers, recovered_constants
    )
    if book is not None:
        data["character_book"] = book
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def build_card_v3(
    character: dict[str, Any],
    entries: list[str] | None,
    *,
    meta: dict[str, Any] | None = None,
    public_lorebooks: list[dict[str, Any]] | None = None,
    recovered_triggers: dict[str, list[str]] | None = None,
    recovered_constants: list[str] | set[str] | None = None,
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
        "system_prompt": character.get("systemPrompt") or "",
        "post_history_instructions": character.get("postHistoryInstructions") or "",
        "tags": character.get("tags") or [],
        "creator": meta.get("creator_name") or character.get("creator") or "",
        "character_version": character.get("characterVersion") or "1.0",
        "alternate_greetings": character.get("alternateGreetings") or [],
        "group_only_greetings": character.get("groupOnlyGreetings") or [],
        "nickname": character.get("nickname") or "",
        "creator_notes_multilingual": character.get("creatorNotesMultilingual")
        if isinstance(character.get("creatorNotesMultilingual"), dict)
        else {},
        "source": _card_sources(character, source_url),
        "assets": copy.deepcopy(character.get("assets"))
        if isinstance(character.get("assets"), list)
        else [{"type": "icon", "uri": "ccdefault:", "name": "main", "ext": "png"}],
        "creation_date": character.get("creationDate")
        if isinstance(character.get("creationDate"), (int, float))
        and not isinstance(character.get("creationDate"), bool)
        else timestamp,
        "modification_date": timestamp,
        "extensions": _card_provenance(meta, source_url, character),
    }
    book = build_character_book(
        entries, public_lorebooks, recovered_triggers, recovered_constants
    )
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

    info = PngInfo()
    for keyword, card in cards.items():
        payload = base64.b64encode(
            json.dumps(card, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        info.add_text(keyword, payload)

    # Keep full resolution, but shrink the longest side until the encoded PNG
    # fits Discord's 10 MB limit (metadata chunks ride along either way).
    while True:
        buffer = BytesIO()
        image.save(buffer, format="PNG", pnginfo=info, optimize=True)
        data = buffer.getvalue()
        if len(data) <= CARD_MAX_BYTES or max(image.size) <= 512:
            return data
        shrunk = int(max(image.size) * 0.85)
        image.thumbnail((shrunk, shrunk), Image.LANCZOS)


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
    recovered_triggers = result.get("recoveredTriggers") or {}
    recovered_constants = result.get("recoveredConstants") or []
    source_url = result.get("url") or ""
    card_v3 = build_card_v3(
        character,
        entries,
        meta=meta,
        public_lorebooks=public_lorebooks,
        recovered_triggers=recovered_triggers,
        recovered_constants=recovered_constants,
        source_url=source_url,
    )
    card_v2 = build_card_v2(
        character,
        entries,
        meta=meta,
        public_lorebooks=public_lorebooks,
        recovered_triggers=recovered_triggers,
        recovered_constants=recovered_constants,
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
    return paths
