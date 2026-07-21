"""chub.ai / CharacterHub native extraction (no browser, no login).

chub is an *open* character archive: a public character's full definition — card
fields plus embedded lorebook — is served with no auth, so extraction is a plain
API read (with a card-PNG fallback). Nothing is gated, so there is no "partial"
path: either the character is public and fully recovered, or it 404s.

This package re-exports the public surface so callers can
``from ripart.providers import chub``.
"""

from __future__ import annotations

from .client import (
    API_BASE,
    AVATARS_BASE,
    CHUB_ORIGIN,
    ChubError,
    character_url,
    fetch_avatar,
    fetch_card_png,
    fetch_node,
    is_chub_url,
    parse_full_path,
    set_trace_level,
)
from .extract import extract_character

__all__ = [
    "API_BASE",
    "AVATARS_BASE",
    "CHUB_ORIGIN",
    "ChubError",
    "character_url",
    "extract_character",
    "fetch_avatar",
    "fetch_card_png",
    "fetch_node",
    "is_chub_url",
    "parse_full_path",
    "set_trace_level",
]
