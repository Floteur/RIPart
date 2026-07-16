"""Pure parsing & formatting utilities (no browser, no I/O beyond file writes).

These helpers turn raw JanitorAI payloads into character cards, lorebook
entries, and trigger messages. They are deliberately side-effect-free so they
can be unit-tested without a browser.
"""

import base64
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ORIGIN = "https://janitorai.com"

# Longest-side cap (px) for stored card portraits - keeps library PNGs small.
CARD_MAX_DIM = 512


def parse_character_id(value: str) -> str:
    match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        value or "",
        re.I,
    )
    if not match:
        raise ValueError(f"no character id found in: {value}")
    return match.group(0)


def html_to_text(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.I)
    text = re.sub(r"</(p|div|li|h\d)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def safe_name(name: str, fallback: str) -> str:
    clean = re.sub(r"[^\w.\- ]+", "_", (name or "").strip() or fallback)
    return clean[:80].strip() or fallback


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def get_system_content(payload: dict[str, Any]) -> str:
    for message in (payload or {}).get("messages") or []:
        if message and message.get("role") == "system" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def strip_wrappers(text: str) -> str:
    out = re.sub(r"^\s*(?:\[[^\]]*\]\s*)+", "", text or "")
    out = re.sub(r"<[^<>\n]*?Persona>[\s\S]*?</[^<>\n]*?Persona>", "\n", out, flags=re.I)
    out = re.sub(r"<Scenario>[\s\S]*?</Scenario>", "\n", out, flags=re.I)
    out = re.sub(r"<Example[^<>\n]*>[\s\S]*?</Example[^<>\n]*>", "\n", out, flags=re.I)
    return out


def extract_card(payload: dict[str, Any]) -> str:
    system = get_system_content(payload)
    for match in re.finditer(r"<([^<>\n]*?)Persona>([\s\S]*?)</[^<>\n]*?Persona>", system, re.I):
        if re.match(r"\s*user", match.group(1), re.I):
            continue
        return match.group(2).strip()
    return ""


def extract_char_name(payload: dict[str, Any]) -> str:
    match = re.search(r"<([^<>\n]*?)Persona>", get_system_content(payload), re.I)
    if not match:
        return ""
    name = re.sub(r"['’]s\s*$", "", match.group(1), flags=re.I).strip()
    return "" if name.lower() == "user" else name


def extract_scenario(payload: dict[str, Any]) -> str:
    match = re.search(r"<Scenario>([\s\S]*?)</Scenario>", get_system_content(payload), re.I)
    return match.group(1).strip() if match else ""


def extract_example(payload: dict[str, Any]) -> str:
    match = re.search(r"<Example[^<>\n]*?>([\s\S]*?)</Example[^<>\n]*?>", get_system_content(payload), re.I)
    return match.group(1).strip() if match else ""


def extract_first_message(payload: dict[str, Any]) -> str:
    for message in (payload or {}).get("messages") or []:
        if message and message.get("role") == "assistant" and isinstance(message.get("content"), str):
            return message["content"].strip()
    return ""


def parse_leaked_definition(text: str) -> dict[str, str]:
    """Split a JanitorLLM injection-leak dump into clean card fields.

    The model reproduces its persona block roughly as it appears in the
    assembled prompt: optionally fenced in a ``` code block, optionally wrapped
    in ``<Name's Persona>…</…Persona>``, with ``<Scenario>`` and
    ``<example_dialogs>`` sub-blocks. This strips the fence/tags and returns
    ``{description, scenario, exampleMessages}`` - lossy by nature (see
    ``definitionSource: reconstructed-jllm``).
    """
    raw = (text or "").strip()
    # Strip one surrounding fenced code block (``` or ```text/markdown/json …).
    raw = re.sub(r"^`{3,}[a-zA-Z0-9_-]*\s*\n", "", raw)
    raw = re.sub(r"\n`{3,}\s*$", "", raw).strip()
    # Drop a leading "<Name's Persona>" opener if the model echoed it.
    raw = re.sub(r"^<[^<>\n]{0,80}?Persona>\s*", "", raw, flags=re.I)

    def _grab(tag: str) -> str:
        match = re.search(rf"<{tag}[^<>\n]*?>([\s\S]*?)</{tag}[^<>\n]*?>", raw, re.I)
        return match.group(1).strip() if match else ""

    scenario = _grab("Scenario")
    example = _grab("example_dialogs") or _grab("Example")

    # Description = everything before the first structural boundary.
    cut = len(raw)
    for pattern in (r"</[^<>\n]{0,80}?Persona>", r"<Scenario[^<>\n]*?>", r"<example_dialogs[^<>\n]*?>", r"<Example[^<>\n]*?>"):
        found = re.search(pattern, raw, re.I)
        if found:
            cut = min(cut, found.start())
    description = raw[:cut].strip()
    return {"description": description, "scenario": scenario, "exampleMessages": example}


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _loose_pattern(value: str) -> str:
    escaped = re.escape(value)
    escaped = re.sub(r"\\\s+", r"\\s+", escaped)
    escaped = re.sub(r"['‘’ʼ]", r"['\u2018\u2019\u02BC]", escaped)
    escaped = re.sub(r'["“”]', r'["\u201C\u201D]', escaped)
    escaped = re.sub(r"[\-–-]", r"[-\u2013\u2014]", escaped)
    return escaped


def split_text_chunks(text: str, max_len: int = 2500, min_len: int = 40) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        paragraphs = [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        sep_len = 2 if current else 0
        if current and current_len + sep_len + len(para) > max_len:
            chunk = "\n\n".join(current)
            if len(chunk) >= min_len:
                chunks.append(chunk)
            current = []
            current_len = 0
        if len(para) > max_len:
            if current:
                chunk = "\n\n".join(current)
                if len(chunk) >= min_len:
                    chunks.append(chunk)
                current = []
                current_len = 0
            for offset in range(0, len(para), max_len):
                piece = para[offset:offset + max_len].strip()
                if len(piece) >= min_len:
                    chunks.append(piece)
            continue
        current.append(para)
        current_len += sep_len + len(para)
    if current:
        chunk = "\n\n".join(current)
        if len(chunk) >= min_len:
            chunks.append(chunk)
    return chunks


def _dedupe_trigger_messages(messages: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for message in messages:
        text = (message or "").strip()
        if len(text) < 40:
            continue
        key = _norm(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def build_lorebook_trigger_messages(
    meta: dict[str, Any] | None,
    card: str,
    public_lorebooks: list[dict[str, Any]] | None = None,
    *,
    chunk_size: int = 2500,
) -> list[str]:
    """Build chat messages that fire as many closed-lorebook keys as possible."""
    meta = meta or {}
    card = (card or "").strip()
    first_message = str(meta.get("first_message") or "").strip()
    primary = f"{card}\n\n{first_message}" if first_message else card
    candidates: list[str] = []
    if primary.strip():
        candidates.append(primary.strip())

    catalog = html_to_text(str(meta.get("description") or ""))
    candidates.extend(split_text_chunks(catalog, chunk_size))

    seen_greetings: set[str] = set()
    for greeting in collect_greetings(meta, first_message):
        text = str(greeting or "").strip()
        if not text:
            continue
        key = _norm(text)
        if key in seen_greetings:
            continue
        seen_greetings.add(key)
        candidates.extend(split_text_chunks(text, chunk_size))

    for book in public_lorebooks or []:
        title = str(book.get("title") or "").strip()
        desc = str(book.get("description") or "").strip()
        if title and desc:
            candidates.extend(split_text_chunks(f"{title}: {desc}", chunk_size))
        elif desc:
            candidates.extend(split_text_chunks(desc, chunk_size))
        elif title:
            candidates.append(title)

    if len(card) > chunk_size:
        candidates.extend(split_text_chunks(card, chunk_size))

    personality = str(meta.get("personality") or "").strip()
    if personality and _norm(personality) != _norm(card):
        candidates.extend(split_text_chunks(personality, chunk_size))

    scenario = str(meta.get("scenario") or "").strip()
    if scenario:
        candidates.extend(split_text_chunks(scenario, chunk_size))

    example_dialogs = str(meta.get("example_dialogs") or "").strip()
    if example_dialogs:
        candidates.extend(split_text_chunks(example_dialogs, chunk_size))

    return _dedupe_trigger_messages(candidates)


def merge_separated_results(separations: list[dict[str, Any]]) -> dict[str, Any]:
    blocks: list[str] = []
    seen: set[str] = set()
    for separated in separations:
        for block in separated.get("entries") or []:
            text = str(block or "").strip()
            if len(text) < 8:
                continue
            key = _norm(text)
            if key in seen:
                continue
            seen.add(key)
            blocks.append(text)
        lorebook_text = str(separated.get("lorebookText") or "").strip()
        if lorebook_text and not separated.get("entries"):
            for block in re.split(r"\n\s*\n", lorebook_text):
                text = block.strip()
                if len(text) < 8:
                    continue
                key = _norm(text)
                if key in seen:
                    continue
                seen.add(key)
                blocks.append(text)
    lorebook_text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(blocks)).strip()
    return {
        "lorebookText": lorebook_text,
        "entries": blocks,
    }


def separate(payload: dict[str, Any], known_card: str = "", public_contents: list[str] | None = None) -> dict[str, Any]:
    system_content = get_system_content(payload)
    text = strip_wrappers(system_content)
    if known_card:
        known = {_norm(line) for line in known_card.splitlines() if len(_norm(line)) >= 12}
        text = "\n".join(
            line for line in text.splitlines()
            if not (len(_norm(line)) >= 12 and _norm(line) in known)
        )
    for content in public_contents or []:
        needle = (content or "").strip()
        if len(needle) >= 12:
            text = re.sub(_loose_pattern(needle), "\n", text, flags=re.I)
    lorebook_text = re.sub(r"[ \t]+\n", "\n", text)
    lorebook_text = re.sub(r"\n{3,}", "\n\n", lorebook_text).strip()
    if not lorebook_text:
        # Janitor often injects closed lorebook entries inside Scenario/Example
        # wrappers instead of after the persona blocks; recover when nothing remains.
        wrapped_parts: list[str] = []
        for pattern in (
            r"<Scenario>([\s\S]*?)</Scenario>",
            r"<Example[^<>\n]*>([\s\S]*?)</Example[^<>\n]*>",
        ):
            for match in re.finditer(pattern, system_content, re.I):
                part = match.group(1).strip()
                if part:
                    wrapped_parts.append(part)
        if wrapped_parts:
            lorebook_text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(wrapped_parts)).strip()
            for content in public_contents or []:
                needle = (content or "").strip()
                if len(needle) >= 12:
                    lorebook_text = re.sub(_loose_pattern(needle), "\n", lorebook_text, flags=re.I)
            lorebook_text = re.sub(r"[ \t]+\n", "\n", lorebook_text)
            lorebook_text = re.sub(r"\n{3,}", "\n\n", lorebook_text).strip()
    return {
        "systemContent": system_content,
        "lorebookText": lorebook_text,
        "entries": [block.strip() for block in re.split(r"\n\s*\n", lorebook_text) if block.strip()],
    }


def collect_greetings(meta: dict[str, Any] | None, captured_first: str = "") -> list[str]:
    out: list[str] = []

    def push(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)

    if meta:
        for value in meta.get("first_messages") or []:
            push(value)
        push(meta.get("first_message"))
        for value in meta.get("alternate_greetings") or []:
            push(value)
    if not out:
        push(captured_first)
    return out


def is_card_public(meta: dict[str, Any] | None) -> bool:
    return bool(meta and meta.get("showdefinition") and ((meta.get("personality") or "").strip() or (meta.get("scenario") or "").strip()))


def build_character(meta: dict[str, Any] | None, payload: dict[str, Any] | None, avatar_base64: str = "", card: str = "") -> dict[str, Any]:
    meta = meta or {}
    greetings = collect_greetings(meta, extract_first_message(payload or {}))
    public = is_card_public(meta)
    return {
        "name": extract_char_name(payload or {}) or meta.get("name") or "",
        "avatarBase64": avatar_base64 or "",
        "description": (meta.get("personality") or "").strip() if public else (extract_card(payload or {}) or card or ""),
        "personality": "",
        "scenario": (meta.get("scenario") or "").strip() if public else (extract_scenario(payload or {}) or meta.get("scenario") or ""),
        "firstMessage": greetings[0] if greetings else "",
        "alternateGreetings": greetings[1:],
        "exampleMessages": (meta.get("example_dialogs") or "").strip() if public else extract_example(payload or {}),
        "creatorNotes": meta.get("description") or "",
        "tags": meta.get("custom_tags") or [],
        "definitionSource": "janitor" if public else "reconstructed",
    }


def build_world_info(raw_entries: list[dict[str, Any]]) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    uid = 0
    for raw in raw_entries:
        content = str(raw.get("content") or raw.get("text") or "").strip()
        if not content:
            continue
        key = raw.get("key") or raw.get("keys") or raw.get("keywords") or []
        secondary = raw.get("keysecondary") or raw.get("secondary_keys") or raw.get("keySecondary") or []
        if isinstance(key, str):
            key = [x.strip() for x in key.split(",") if x.strip()]
        if isinstance(secondary, str):
            secondary = [x.strip() for x in secondary.split(",") if x.strip()]
        order = raw.get("order", raw.get("priority", raw.get("insertion_order", 100)))
        entries[str(uid)] = {
            "uid": uid,
            "key": key if isinstance(key, list) else [],
            "keysecondary": secondary if isinstance(secondary, list) else [],
            "comment": str(raw.get("comment") or raw.get("title") or raw.get("name") or f"Entry {uid}").strip(),
            "content": content,
            "constant": raw.get("constant") is True,
            "selective": raw.get("constant") is not True,
            "order": order if isinstance(order, (int, float)) else 100,
            "position": raw.get("position") if isinstance(raw.get("position"), int) else 0,
            "disable": raw.get("enabled") is False,
            "displayIndex": uid,
            "addMemo": True,
            "group": "",
            "groupOverride": False,
            "groupWeight": raw.get("groupWeight") if isinstance(raw.get("groupWeight"), int) else 100,
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
            "selectiveLogic": raw.get("selectiveLogic") if isinstance(raw.get("selectiveLogic"), int) else 0,
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


def _public_book_entries(public_lorebooks: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Flatten accessible public-lorebook ``worldInfo`` entries (with real keys)."""
    out: list[dict[str, Any]] = []
    for book in public_lorebooks or []:
        entries = ((book.get("worldInfo") or {}).get("entries") or {})
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
) -> dict[str, Any]:
    """Assemble a Tavern ``character_book`` from all available lorebook data.

    Two sources, deduped by content: (1) accessible public-lorebook entries,
    which keep their real trigger ``keys``/``secondary_keys``/``comment``; and
    (2) the extracted closed-lorebook blocks, which are stored ``constant``
    (always active) with empty keys since we can't recover their trigger words.
    Includes the V3-required ``use_regex``/``extensions`` fields (harmless for V2).
    """
    book_entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(content: str, *, keys, secondary, comment, constant, enabled):
        text = str(content or "").strip()
        if not text:
            return
        norm = _norm(text)
        if norm in seen:
            return
        seen.add(norm)
        order = len(book_entries)
        book_entries.append({
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
        })

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
    """Assemble a Tavern ``chara_card_v2`` dict with the lorebook embedded."""
    meta = meta or {}
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
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
            "character_book": build_character_book(entries, public_lorebooks),
        },
    }


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
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
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
            "character_book": build_character_book(entries, public_lorebooks),
        },
    }


def encode_card_png(avatar: str | bytes | None, cards: dict[str, dict[str, Any]]) -> bytes:
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
        payload = base64.b64encode(json.dumps(card, ensure_ascii=False).encode("utf-8")).decode("ascii")
        info.add_text(keyword, payload)

    buffer = BytesIO()
    image.save(buffer, format="PNG", pnginfo=info, optimize=True)
    return buffer.getvalue()


def _update_library_index(library_dir: Path, character_id: str, result: dict[str, Any], entries: list[str]) -> None:
    index_path = library_dir / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
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
        "extractedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "file": f"{character_id}.png",
    }
    write_json(index_path, index)


def save_to_library(library_dir: Path, character_id: str, result: dict[str, Any]) -> dict[str, str]:
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
    card_v3 = build_card_v3(character, entries, meta=meta, public_lorebooks=public_lorebooks, source_url=source_url)
    card_v2 = build_card_v2(character, entries, meta=meta, public_lorebooks=public_lorebooks, source_url=source_url)

    library_dir.mkdir(parents=True, exist_ok=True)
    png_path = library_dir / f"{character_id}.png"
    png_path.write_bytes(
        encode_card_png(character.get("avatarBase64"), {"ccv3": card_v3, "chara": card_v2})
    )

    _update_library_index(library_dir, character_id, result, entries)
    return {"png": str(png_path)}
