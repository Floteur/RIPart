"""Top-level spicychat.ai extraction: build a RIPart ``result`` dict from a URL.

spicychat exposes a character's definition directly when its creator left the
definition public (``definition_visible: true``): ``persona`` is the character
description, ``dialogue`` the example messages, ``scenario`` the scenario. Those
map straight onto a full card. When the definition is gated we can still recover
the public surface (``greeting`` + metadata + avatar) as a *partial* card —
marked ``spicychat-partial`` — exactly like clank's gated path; a verbatim leak
is a future addition.

Lorebooks attached to a character surface as metadata only (name + entry count);
their entries are never returned by the read API, so they are noted but not
embedded.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .client import (
    SpicyChatError,
    character_url,
    fetch_avatar,
    parse_character_id,
)
from .read import get_character


def _noop(_message: str) -> None:
    pass


def _as_text(value: Any) -> str:
    """Coerce a persona/dialogue/scenario field (str, or list of turns) to text."""
    if isinstance(value, list):
        return "\n\n".join(str(v).strip() for v in value if str(v).strip()).strip()
    return str(value or "").strip()


def _lorebook_note(lorebooks: list[dict[str, Any]]) -> str:
    """A creator-notes blurb describing attached lorebooks (entries are gated)."""
    lines = []
    for book in lorebooks:
        if not isinstance(book, dict):
            continue
        name = str(book.get("name") or "lorebook").strip()
        count = book.get("num_entries")
        desc = str(book.get("description") or "").strip()
        suffix = f" — {desc}" if desc and desc != name else ""
        lines.append(f"- {name} ({count} entries){suffix}")
    if not lines:
        return ""
    return (
        "--- Attached lorebook(s) ---\n"
        "spicychat does not expose lorebook entry contents via its API, so only "
        "the metadata below was recovered:\n" + "\n".join(lines)
    )


def extract_character(
    url: str,
    *,
    log: Callable[[str], None] = _noop,
) -> dict[str, Any]:
    """Rip a spicychat.ai character into a RIPart ``result`` dict.

    ``url`` may be a ``spicychat.ai/chatbot/<uuid>`` URL, a ``.../characters/<uuid>``
    URL, or a bare UUID. The result is shaped for
    :func:`ripart.common.cards.save_to_library`. No login is required for a
    character whose definition is public.
    """
    character_id = parse_character_id(url)
    if not character_id:
        raise SpicyChatError(
            "not a spicychat.ai character URL "
            "(expected spicychat.ai/chatbot/<uuid> or a bare UUID)",
            400,
        )

    log(f"fetching character {character_id} …")
    data = get_character(character_id)

    name = str(data.get("name") or "Unknown").strip()
    visible = bool(data.get("definition_visible"))
    persona = _as_text(data.get("persona"))
    example = _as_text(data.get("dialogue"))
    scenario = _as_text(data.get("scenario"))
    greeting = _as_text(data.get("greeting"))
    tags = [str(t) for t in (data.get("tags") or []) if str(t).strip()]
    lorebooks = data.get("lorebooks") if isinstance(data.get("lorebooks"), list) else []
    avatar = fetch_avatar(data.get("avatar_url"))

    notes_parts: list[str] = []
    if visible and persona:
        source = "spicychat-api"
        log(f"public definition: {len(persona)} chars")
    else:
        source = "spicychat-partial"
        persona = ""  # never trust a stray field when the flag says gated
        example = ""
        scenario = ""
        notes_parts.append(
            "--- Note ---\n"
            "This character's definition is gated on spicychat.ai "
            "(definition_visible=false); only public metadata (name, greeting, "
            "tags, avatar) was recovered. The persona/example dialogue are not "
            "exposed by the API and stay empty."
        )
        log("definition gated — saving a partial card")

    lore_note = _lorebook_note(lorebooks)
    if lore_note:
        notes_parts.append(lore_note)

    character = {
        "name": name,
        "avatarBase64": avatar,
        "description": persona,
        "personality": "",
        "scenario": scenario,
        "firstMessage": greeting,
        "alternateGreetings": [],
        "exampleMessages": example,
        "creatorNotes": "\n\n".join(notes_parts),
        "tags": tags,
        "definitionSource": source,
    }

    meta = {
        "name": name,
        "creator_name": str(data.get("creator_username") or "").strip(),
        "creator_id": str(data.get("creator_user_id") or ""),
        "is_nsfw": bool(data.get("is_nsfw")),
        "showdefinition": visible,
    }

    return {
        "url": character_url(character_id),
        "characterId": character_id,
        "characterName": name,
        "character": character,
        "meta": meta,
        "publicLorebooks": [],
        "entries": [],
        "lorebookText": "",
        "leakRaw": "",
        "diagnostics": {
            "characterId": character_id,
            "definitionVisible": visible,
            "definitionChars": len(persona),
            "exampleChars": len(example),
            "scenarioChars": len(scenario),
            "greetingChars": len(greeting),
            "tags": tags,
            "lorebookCount": len(lorebooks),
            "tokenCount": data.get("token_count"),
        },
    }
