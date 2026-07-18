"""clank.world native extraction (no browser).

clank gates a character's real definition: the tRPC read API exposes only public
metadata, and the full definition lives only in the *system prompt* clank sends
the model at generation time. The leak recovers it verbatim with an **echo
proxy** — an OpenAI-compatible endpoint that echoes the request body back as the
assistant reply — whose ``developer`` message is the character's system prompt.

Auth is the browser's ``next-auth`` session cookie, persisted to a gitignored
``.clank-session.json`` at the package root. This package re-exports the public
surface so callers can ``from ripart.providers import clank as ck``.
"""

from __future__ import annotations

from ...common.echo import DEFAULT_ECHO_BASE_URL
from .client import (
    CLANK_BASE,
    CLANK_ORIGIN,
    SESSION_FILE,
    ClankError,
    clear_session,
    fetch_avatar,
    has_session,
    is_clank_url,
    load_session,
    parse_chat_id,
    parse_scene_id,
    parse_target,
    set_session,
    set_trace_level,
    use_session,
)
from .echo import find_echo_body, split_definition
from .extract import extract_chat, extract_story
from .lorebook import (
    build_trigger_messages,
    dump_lorebook,
    extract_injections,
    trigger_generation,
)
from .trpc import (
    FEED_SORTS,
    get_chat_info,
    get_chat_messages,
    get_story,
    iter_stories,
    list_stories,
    resolve_character_chat,
    story_character_url,
)

__all__ = [
    "CLANK_BASE",
    "CLANK_ORIGIN",
    "DEFAULT_ECHO_BASE_URL",
    "FEED_SORTS",
    "SESSION_FILE",
    "ClankError",
    "build_trigger_messages",
    "clear_session",
    "dump_lorebook",
    "extract_chat",
    "extract_injections",
    "extract_story",
    "fetch_avatar",
    "find_echo_body",
    "get_chat_info",
    "get_chat_messages",
    "get_story",
    "has_session",
    "is_clank_url",
    "iter_stories",
    "list_stories",
    "load_session",
    "parse_chat_id",
    "parse_scene_id",
    "parse_target",
    "resolve_character_chat",
    "set_session",
    "set_trace_level",
    "split_definition",
    "story_character_url",
    "trigger_generation",
    "use_session",
]
