"""Top-level clank.world extraction: build a RIPart ``result`` dict from a URL."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...common.echo import DEFAULT_ECHO_BASE_URL
from .client import (
    CLANK_BASE,
    ClankError,
    fetch_avatar,
    has_session,
    parse_scene_id,
    parse_target,
)
from .echo import (
    _greeting_message,
    _norm_ws,
    _system_message,
    _system_message_raw,
    find_echo_body,
    split_definition,
)
from .lorebook import dump_lorebook, leak_chat_definition
from .trpc import get_chat_info, get_chat_messages, get_story, resolve_character_chat


def _noop(_message: str) -> None:
    pass


def _agent_and_story(info: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    agents = info.get("agent_data") if isinstance(info.get("agent_data"), list) else []
    agent = agents[0] if agents and isinstance(agents[0], dict) else {}
    story = info.get("story_data") if isinstance(info.get("story_data"), dict) else {}
    return agent, story


def _build_result(
    chat_id: str,
    url: str,
    info: dict[str, Any],
    parsed: dict[str, str] | None,
    greeting: str,
    *,
    scene: dict[str, Any] | None = None,
    keep_boilerplate: bool = False,
    leak_error: str = "",
) -> dict[str, Any]:
    agent, story = _agent_and_story(info)
    scene = scene or {}
    name = str(agent.get("name") or story.get("title") or "Unknown").strip()

    # Greetings: prefer the verbatim echo greeting for first_mes; take any extra
    # scene greetings as alternates.
    greetings = [
        g
        for g in (scene.get("initial_message") or story.get("initial_message") or [])
        if str(g).strip()
    ]
    if not greeting:
        greeting = greetings[0] if greetings else ""
    alternates = [
        g for g in greetings if str(g).strip() and _norm_ws(g) != _norm_ws(greeting)
    ]

    # Scenario: the scene's public setup prompt.
    scenario = str(scene.get("prompt") or story.get("description") or "").strip()

    # Tags from the scene.
    tags = scene.get("tags") if isinstance(scene.get("tags"), list) else []

    creator = (
        agent.get("created_by") if isinstance(agent.get("created_by"), dict) else {}
    )
    avatar = fetch_avatar(agent.get("image") or story.get("image"))

    notes_parts: list[str] = []

    if parsed:
        definition = parsed["definition"]
        example = parsed["example"]
        if keep_boilerplate and parsed.get("boilerplate"):
            notes_parts.append(
                f"--- clank system boilerplate ---\n{parsed['boilerplate']}"
            )
        source = "clank-echo-leak"
    else:
        definition = ""
        example = ""
        notes_parts.append(
            "--- Note ---\nThe character definition is gated on clank.world and was not "
            "leaked; only public metadata (name, avatar, story blurb) is present. "
            "Configure the echo proxy on this chat and re-run with --leak."
        )
        source = "clank-partial"

    character = {
        "name": name,
        "avatarBase64": avatar,
        "description": definition,
        "personality": "",
        "scenario": scenario,
        "firstMessage": greeting,
        "alternateGreetings": alternates,
        "exampleMessages": example,
        "creatorNotes": "\n\n".join(notes_parts),
        "tags": [str(t) for t in tags],
        "definitionSource": source,
    }
    if parsed:
        character["reconstruction"] = {
            "method": "clank-echo-proxy",
            "chars": len(definition),
        }

    meta = {
        "name": name,
        "creator_name": str(
            creator.get("display_name") or creator.get("username") or ""
        ).strip(),
        "creator_id": str(creator.get("user_id") or ""),
        "is_nsfw": bool(scene.get("is_nsfw")),
        "showdefinition": bool(parsed),
    }

    return {
        "url": url
        if url.startswith(("http://", "https://"))
        else f"{CLANK_BASE}/chat/{chat_id}",
        "characterId": str(agent.get("id") or chat_id),
        "characterName": name,
        "character": character,
        "meta": meta,
        "publicLorebooks": [],
        "entries": [],
        "lorebookText": "",
        "leakRaw": _system_message_raw(parsed),
        "diagnostics": {
            "chatId": chat_id,
            "definitionChars": len(character["description"]),
            "exampleChars": len(character["exampleMessages"]),
            "scenarioChars": len(scenario),
            "tags": character["tags"],
            "alternateGreetings": len(alternates),
            "hasEcho": bool(parsed),
            "leakError": leak_error,
        },
    }


def extract_chat(
    url: str,
    *,
    leak: bool = False,
    keep_boilerplate: bool = False,
    echo_base_url: str = DEFAULT_ECHO_BASE_URL,
    trigger_message: str = "hi",
    leak_timeout: int = 60,
    with_lorebook: bool = False,
    lorebook_sleep: float = 3.0,
    max_triggers: int | None = None,
    log: Callable[[str], None] = _noop,
) -> dict[str, Any]:
    """Rip a clank.world chat into a RIPart ``result`` dict.

    Reads the chat's public metadata and, if an echoed prompt is present in the
    history (or ``leak`` triggers one), parses the verbatim character definition
    into the card. The result is shaped for ``ripart.common.cards.save_to_library``.

    * ``leak`` — auto-configure the echo proxy and send a throwaway message to
      force an echo when the history has none (requires the write procedures to
      be wired; see :func:`~ripart.providers.clank.lorebook.leak_chat_definition`).
    * ``keep_boilerplate`` — keep the generic clank RP rules in creator notes.
    """
    if not has_session():
        raise ClankError("no clank.world session - run `rip clank login` first", 401)
    kind, ident = parse_target(url)
    if kind == "chat":
        chat_id = ident
    elif kind == "character":
        log(f"resolving character {ident!r} to an existing chat …")
        chat_id = resolve_character_chat(ident)
        if not chat_id:
            raise ClankError(
                f"no existing chat found for character '{ident}'. Open a chat with them "
                "on clank.world (set the echo proxy as the chat's LLM provider and send "
                "one message), then re-run — or pass the clank.world/chat/<id> URL.",
                404,
            )
        log(f"resolved to chat {chat_id}")
    else:
        raise ClankError(
            "not a clank.world chat or character URL "
            "(expected clank.world/chat/<uuid> or clank.world/@<slug>)",
            400,
        )

    info = get_chat_info(chat_id)
    # Richer scene data (scenario prompt, tags, greetings) when we have a scene id.
    scene_id = (
        ((info.get("story_data") or {}).get("id"))
        if isinstance(info.get("story_data"), dict)
        else None
    )
    scene = get_story(str(scene_id)) if scene_id else {}
    messages = get_chat_messages(chat_id)
    body = find_echo_body(messages)

    leak_error = ""
    if body is None and leak:
        try:
            body = leak_chat_definition(
                chat_id,
                base_url=echo_base_url,
                message=trigger_message,
                timeout=leak_timeout,
                log=log,
            )
            if body is None:
                leak_error = "no echo appeared before the timeout"
        except ClankError as exc:
            leak_error = str(exc)

    parsed = None
    greeting = ""
    if body is not None:
        system_prompt = _system_message(body)
        parsed = split_definition(system_prompt)
        greeting = _greeting_message(body)
        if not parsed["definition"]:
            # Echo present but the prompt didn't match the expected layout.
            leak_error = leak_error or "echoed prompt did not match the expected layout"

    result = _build_result(
        chat_id,
        url,
        info,
        parsed,
        greeting,
        scene=scene,
        keep_boilerplate=keep_boilerplate,
        leak_error=leak_error,
    )

    # Optional: fire the character's lorebook via keyword triggers built from the
    # card's own text, and fold the recovered entries into the result.
    if with_lorebook and body is not None and parsed:
        try:
            entries = dump_lorebook(
                chat_id,
                description=parsed["definition"],
                scenario=str(scene.get("prompt") or ""),
                first_mes=greeting,
                base_body=body,
                sleep=lorebook_sleep,
                max_triggers=max_triggers,
                log=log,
            )
            # ``save_to_library`` consumes this canonical field when embedding
            # recovered lore into the card and persistent observation record.
            result["entries"] = entries
            result["lorebookEntries"] = entries
            result["diagnostics"]["lorebookEntries"] = len(entries)
            if entries:
                note = "--- Recovered lorebook entries ---\n" + "\n\n".join(entries)
                character = result.get("character") or {}
                character["creatorNotes"] = (
                    character.get("creatorNotes", "") + "\n\n" + note
                ).strip()
        except ClankError as exc:
            result["diagnostics"]["lorebookError"] = str(exc)

    return result


def extract_story(
    scene: str | dict[str, Any],
    *,
    log: Callable[[str], None] = _noop,
) -> dict[str, Any]:
    """Build a **partial** card from a scene's public data — no chat, no echo.

    Captures everything clank exposes publicly (name, scenario, greeting(s),
    tags, avatar, creator, sibling characters, audio flag) but NOT the gated
    character definition (``description`` stays empty, ``definitionSource`` is
    ``clank-partial``). Accepts a scene UUID, a ``?scene=<uuid>`` URL, or a feed
    ``item`` dict (from :func:`~ripart.providers.clank.trpc.list_stories`). Use the
    echo leak on a chat to fill in the definition.
    """
    if isinstance(scene, dict):
        story = scene if scene.get("characters") is not None else {}
        scene_id = str(scene.get("id") or "")
        if not story and scene_id:
            story = get_story(scene_id)
    else:
        scene_id = parse_scene_id(scene) or str(scene)
        story = get_story(scene_id)
    if not story:
        raise ClankError(f"scene not found: {scene_id}", 404)

    chars = story.get("characters") if isinstance(story.get("characters"), list) else []
    primary = chars[0] if chars and isinstance(chars[0], dict) else {}
    name = str(primary.get("display_name") or story.get("title") or "Unknown").strip()
    log(f"partial card: {name!r} (scene {scene_id})")

    greetings = [g for g in (story.get("initial_message") or []) if str(g).strip()]
    scenario = str(story.get("prompt") or "").strip()
    tags = [str(t) for t in (story.get("tags") or [])]
    avatar = fetch_avatar(primary.get("image_url") or story.get("image_url"))

    creator = (
        primary.get("created_by") if isinstance(primary.get("created_by"), dict) else {}
    )
    if not creator:
        creator = {
            "user_id": story.get("created_by_user_id"),
            "username": story.get("created_by_username"),
            "display_name": story.get("created_by_display_name"),
        }

    notes_parts: list[str] = []
    others = [str(c.get("display_name")) for c in chars[1:] if isinstance(c, dict)]
    if others:
        notes_parts.append("Other characters in this scene: " + ", ".join(others))
    if story.get("audio"):
        notes_parts.append("This scene has narration/voice audio (not downloaded).")
    notes_parts.append(
        "--- Note ---\nCharacter definition is gated on clank.world and is NOT included "
        "here. Open a chat with this character (set the echo proxy as the chat's LLM "
        "provider, send one message), then run `rip clank extract` for the full definition."
    )

    character = {
        "name": name,
        "avatarBase64": avatar,
        "description": "",
        "personality": "",
        "scenario": scenario,
        "firstMessage": greetings[0] if greetings else "",
        "alternateGreetings": greetings[1:],
        "exampleMessages": "",
        "creatorNotes": "\n\n".join(notes_parts),
        "tags": tags,
        "definitionSource": "clank-partial",
    }
    character_id = str(primary.get("id") or scene_id)
    username = str(primary.get("username") or "").strip()
    return {
        "url": f"{CLANK_BASE}/@{username}"
        if username
        else f"{CLANK_BASE}/?scene={scene_id}",
        "characterId": character_id,
        "characterName": name,
        "character": character,
        "meta": {
            "name": name,
            "creator_name": str(
                creator.get("display_name") or creator.get("username") or ""
            ).strip(),
            "creator_id": str(creator.get("user_id") or ""),
            "is_nsfw": bool(story.get("is_nsfw")),
            "showdefinition": False,
        },
        "publicLorebooks": [],
        "entries": [],
        "lorebookText": "",
        "leakRaw": "",
        "diagnostics": {
            "sceneId": scene_id,
            "definitionChars": 0,
            "scenarioChars": len(scenario),
            "tags": tags,
            "alternateGreetings": max(0, len(greetings) - 1),
            "otherCharacters": others,
            "hasAudio": bool(story.get("audio")),
            "hasEcho": False,
        },
    }
