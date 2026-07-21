"""spicychat.ai native extraction (no browser).

spicychat (a NextDayAI platform) serves a character's definition directly from
its REST API when the creator left it public (``definition_visible: true``):
``persona`` / ``dialogue`` / ``scenario`` map straight onto a full card. Gated
definitions yield a partial card (public metadata + greeting) — a verbatim leak
is a future addition. No login is needed to read public definitions: a
self-generated ``x-guest-userid`` is enough. Optional refresh-token login adds a
bearer header (for NSFW visibility / rate limits) but does not un-gate anything.

This package re-exports the public surface so callers can
``from ripart.providers import spicychat as sc``.
"""

from __future__ import annotations

from .client import (
    APP_ID,
    CDN_BASE,
    NDAPI_BASE,
    SESSION_FILE,
    SPICYCHAT_ORIGIN,
    SpicyChatError,
    authenticate,
    character_url,
    clear_session,
    fetch_avatar,
    has_token,
    is_spicychat_url,
    load_session,
    parse_character_id,
    set_refresh_token,
    set_trace_level,
    token_expiry,
    use_session,
)
from .extract import extract_character
from .leak import DEFAULT_LEAK_MODEL, DEFAULT_LEAK_PROMPT, leak_definition
from .read import get_character, search_characters

__all__ = [
    "APP_ID",
    "CDN_BASE",
    "DEFAULT_LEAK_MODEL",
    "DEFAULT_LEAK_PROMPT",
    "NDAPI_BASE",
    "SESSION_FILE",
    "SPICYCHAT_ORIGIN",
    "SpicyChatError",
    "authenticate",
    "character_url",
    "clear_session",
    "extract_character",
    "fetch_avatar",
    "get_character",
    "has_token",
    "is_spicychat_url",
    "leak_definition",
    "load_session",
    "parse_character_id",
    "search_characters",
    "set_refresh_token",
    "set_trace_level",
    "token_expiry",
    "use_session",
]
