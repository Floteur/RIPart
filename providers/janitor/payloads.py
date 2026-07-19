"""JanitorAI payload parsing — turn raw JanitorAI prompt payloads into cards.

Side-effect-free string wrangling: extract the character card / scenario /
example dialogue out of the assembled system prompt, separate injected
closed-lorebook blocks from the known card text, and build trigger messages that
fire as many closed-lorebook keys as possible. The provider-agnostic card and
text utilities live in :mod:`ripart.common`.
"""

from __future__ import annotations

import re
from typing import Any

from stopwordsiso import stopwords

from ...common.text import html_to_text, normalize_user_placeholder, split_text_chunks
from ...common.text import norm as _norm

ORIGIN = "https://janitorai.com"
ENGLISH_STOPWORDS = stopwords(["en"])


def parse_character_id(value: str) -> str:
    match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        value or "",
        re.I,
    )
    if not match:
        raise ValueError(f"no character id found in: {value}")
    return match.group(0)


def get_system_content(payload: dict[str, Any]) -> str:
    for message in (payload or {}).get("messages") or []:
        if (
            message
            and message.get("role") == "system"
            and isinstance(message.get("content"), str)
        ):
            return message["content"]
    return ""


def strip_wrappers(text: str) -> str:
    out = re.sub(r"^\s*(?:\[[^\]]*\]\s*)+", "", text or "")
    out = re.sub(
        r"<[^<>\n]*?Persona>[\s\S]*?</[^<>\n]*?Persona>", "\n", out, flags=re.I
    )
    out = re.sub(r"<Scenario>[\s\S]*?</Scenario>", "\n", out, flags=re.I)
    out = re.sub(r"<Example[^<>\n]*>[\s\S]*?</Example[^<>\n]*>", "\n", out, flags=re.I)
    return out


def extract_card(payload: dict[str, Any]) -> str:
    system = get_system_content(payload)
    for match in re.finditer(
        r"<([^<>\n]*?)Persona>([\s\S]*?)</[^<>\n]*?Persona>", system, re.I
    ):
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
    match = re.search(
        r"<Scenario>([\s\S]*?)</Scenario>", get_system_content(payload), re.I
    )
    return match.group(1).strip() if match else ""


def extract_example(payload: dict[str, Any]) -> str:
    match = re.search(
        r"<Example[^<>\n]*?>([\s\S]*?)</Example[^<>\n]*?>",
        get_system_content(payload),
        re.I,
    )
    return match.group(1).strip() if match else ""


def extract_first_message(payload: dict[str, Any]) -> str:
    for message in (payload or {}).get("messages") or []:
        if (
            message
            and message.get("role") == "assistant"
            and isinstance(message.get("content"), str)
        ):
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
    for pattern in (
        r"</[^<>\n]{0,80}?Persona>",
        r"<Scenario[^<>\n]*?>",
        r"<example_dialogs[^<>\n]*?>",
        r"<Example[^<>\n]*?>",
    ):
        found = re.search(pattern, raw, re.I)
        if found:
            cut = min(cut, found.start())
    description = raw[:cut].strip()
    return {
        "description": description,
        "scenario": scenario,
        "exampleMessages": example,
    }


def _loose_pattern(value: str) -> str:
    escaped = re.escape(value)
    escaped = re.sub(r"\\\s+", r"\\s+", escaped)
    escaped = re.sub(r"['‘’ʼ]", r"['‘’ʼ]", escaped)
    escaped = re.sub(r'["“”]', r'["“”]', escaped)
    escaped = re.sub(r"[\-–-]", r"[-–—]", escaped)
    return escaped


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


def build_trigger_search_messages(entries: list[str]) -> list[tuple[str, str]]:
    """Return narrow candidate-key probes for recovered private lore.

    These are deliberately separate from the normal broad trigger passes.  A
    probe contains one candidate phrase only, so an entry observed in its
    response has a useful, reproducible activation key rather than a guess
    copied from a large card chunk.
    """
    candidate_groups: list[list[str]] = []
    for entry in entries:
        text = str(entry or "").strip()
        if not text:
            continue
        candidates: list[str] = []
        # Headings and title-cased phrases are often author-entered lore keys.
        for line in text.splitlines():
            heading = line.strip().lstrip("#•*- ").rstrip(":")
            if not heading:
                continue
            if heading.isupper() or re.fullmatch(
                r"(?:[A-Z][\w'-]*)(?:\s+[A-Z][\w'-]*){1,4}", heading
            ):
                candidates.append(heading)
        # Proper names and distinctive words cover entries without a useful
        # heading.  Keep this deliberately short: a broad sweep wastes probes
        # on generic vocabulary before other recovered entries are tested.
        for phrase in re.findall(
            r"\b[A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*)+\b", text
        ):
            candidates.append(phrase)
        words: list[str] = []
        for word in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text):
            if word.lower() not in ENGLISH_STOPWORDS:
                words.append(word)
        candidates.extend(words[:3])

        group: list[str] = []
        seen_group: set[str] = set()
        for candidate in candidates:
            key = _norm(candidate)
            if key and key not in seen_group:
                seen_group.add(key)
                group.append(candidate.strip())
        if group:
            candidate_groups.append(group)

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    while any(candidate_groups):
        for group in candidate_groups:
            if not group:
                continue
            candidate = group.pop(0)
            key = _norm(candidate)
            if key in seen:
                continue
            seen.add(key)
            # Do not add conversational filler: it can itself accidentally
            # match a broad lore key. The candidate is the complete user turn.
            out.append((candidate, candidate))
    return out


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
                blocks.append(normalize_user_placeholder(text))
    lorebook_text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(blocks)).strip()
    return {
        "lorebookText": lorebook_text,
        "entries": blocks,
    }


def separate(
    payload: dict[str, Any],
    known_card: str = "",
    public_contents: list[str] | None = None,
) -> dict[str, Any]:
    system_content = get_system_content(payload)
    text = strip_wrappers(system_content)
    if known_card:
        known = {
            _norm(line) for line in known_card.splitlines() if len(_norm(line)) >= 12
        }
        text = "\n".join(
            line
            for line in text.splitlines()
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
            lorebook_text = re.sub(
                r"\n{3,}", "\n\n", "\n\n".join(wrapped_parts)
            ).strip()
            for content in public_contents or []:
                needle = (content or "").strip()
                if len(needle) >= 12:
                    lorebook_text = re.sub(
                        _loose_pattern(needle), "\n", lorebook_text, flags=re.I
                    )
            lorebook_text = re.sub(r"[ \t]+\n", "\n", lorebook_text)
            lorebook_text = re.sub(r"\n{3,}", "\n\n", lorebook_text).strip()
    lorebook_text = normalize_user_placeholder(lorebook_text)
    return {
        "systemContent": system_content,
        "lorebookText": lorebook_text,
        "entries": [
            block.strip()
            for block in re.split(r"\n\s*\n", lorebook_text)
            if block.strip()
        ],
    }


def collect_greetings(
    meta: dict[str, Any] | None, captured_first: str = ""
) -> list[str]:
    out: list[str] = []

    def push(value: Any) -> None:
        text = normalize_user_placeholder(str(value or "").strip())
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
    return bool(
        meta
        and meta.get("showdefinition")
        and (
            (meta.get("personality") or "").strip()
            or (meta.get("scenario") or "").strip()
        )
    )


def build_character(
    meta: dict[str, Any] | None,
    payload: dict[str, Any] | None,
    avatar_base64: str = "",
    card: str = "",
) -> dict[str, Any]:
    meta = meta or {}
    greetings = collect_greetings(meta, extract_first_message(payload or {}))
    public = is_card_public(meta)
    return {
        "name": extract_char_name(payload or {}) or meta.get("name") or "",
        "avatarBase64": avatar_base64 or "",
        "description": normalize_user_placeholder((meta.get("personality") or "").strip())
        if public
        else normalize_user_placeholder(extract_card(payload or {}) or card or ""),
        "personality": "",
        "scenario": normalize_user_placeholder((meta.get("scenario") or "").strip())
        if public
        else normalize_user_placeholder(
            extract_scenario(payload or {}) or meta.get("scenario") or ""
        ),
        "firstMessage": greetings[0] if greetings else "",
        "alternateGreetings": greetings[1:],
        "exampleMessages": normalize_user_placeholder(
            (meta.get("example_dialogs") or "").strip()
        )
        if public
        else normalize_user_placeholder(extract_example(payload or {})),
        "creatorNotes": normalize_user_placeholder(meta.get("description") or ""),
        "tags": meta.get("custom_tags") or [],
        "definitionSource": "janitor" if public else "reconstructed",
    }
