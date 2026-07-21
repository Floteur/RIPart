"""JanitorAI extraction (browser-driven).

JanitorAI has no public definition API, so this provider drives a real headless
Chromium via Botasaurus (:mod:`~ripart.providers.janitor.browser_tasks`) to read
the assembled prompt, then parses it into a card
(:mod:`~ripart.providers.janitor.payloads`). The login profile and exported
session live in RIPart's application-state directory, alongside the other
providers' credentials.

This package re-exports the Botasaurus task entry points and the pure payload
parsers so callers can ``from ripart.providers import janitor``.
"""

from __future__ import annotations

from .browser_tasks import (
    extract_task,
    import_session_task,
    inspect_task,
    login_task,
    lorebook_task,
    recent_task,
    status_task,
)
from .payloads import (
    ORIGIN,
    build_character,
    build_lorebook_trigger_messages,
    extract_card,
    is_card_public,
    merge_separated_results,
    parse_character_id,
    parse_leaked_definition,
    separate,
)

__all__ = [
    "ORIGIN",
    "build_character",
    "build_lorebook_trigger_messages",
    "extract_card",
    "extract_task",
    "import_session_task",
    "inspect_task",
    "is_card_public",
    "login_task",
    "lorebook_task",
    "merge_separated_results",
    "parse_character_id",
    "parse_leaked_definition",
    "recent_task",
    "separate",
    "status_task",
]
