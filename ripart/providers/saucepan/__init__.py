"""Saucepan (saucepan.ai) native extraction.

Unlike the JanitorAI path, Saucepan needs no browser: its companion definition
is available directly from the authenticated REST API (though it ships shuffled +
decoy-padded — see :mod:`~ripart.providers.saucepan.fragments`). The gated
example dialogue / advanced prompt are recovered with a leak
(:mod:`~ripart.providers.saucepan.leak`): a model dump, or a verbatim echo-proxy
pull. The bearer token is persisted in RIPart's application-state directory and
reused by every command.

This package re-exports the public surface (and a few internals used by the test
suite) so callers can ``from ripart.providers import saucepan as sp``.
"""

from __future__ import annotations

# Shared echo constants (kept importable at the old ``saucepan`` names).
from ...common.echo import DEFAULT_ECHO_BASE_URL, ECHO_MODEL
from ...common.text import strip_code_fence as _strip_code_fence
from .client import (
    SAUCEPAN_BASE,
    SAUCEPAN_ORIGIN,
    SAUCEPAN_UA,
    TOKEN_FILE,
    SaucepanError,
    _companion_creator,
    authenticate,
    clear_token,
    fetch_avatar,
    has_token,
    is_saucepan_url,
    load_token,
    login,
    parse_companion_id,
    set_token,
    set_trace_level,
    search_companions,
    token_expiry,
    use_token,
)
from .extract import (
    _apply_echo_leak,
    _apply_leak,
    _split_echo_definition,
    _split_example_section,
    extract_companion,
)
from .fragments import _U32, _fragment_hash, assemble_fragments
from .leak import (
    DEFAULT_LEAK_PROMPT,
    _looks_like_definition,
    _looks_like_refusal,
    archive_chat,
    cancel_generation,
    create_chat,
    find_echo_config,
    get_provider_config,
    leak_definition,
    leak_definition_via_echo,
    list_provider_configs,
    resolve_provider_config,
    set_provider_prompt,
    update_provider_config,
)
from .lorebook import (
    _clean_lore_text,
    _lorebook_world_info,
    fetch_companion_lorebooks,
    fetch_lorebook,
)

__all__ = [
    "DEFAULT_ECHO_BASE_URL",
    "DEFAULT_LEAK_PROMPT",
    "ECHO_MODEL",
    "SAUCEPAN_BASE",
    "SAUCEPAN_ORIGIN",
    "SAUCEPAN_UA",
    "TOKEN_FILE",
    "SaucepanError",
    "archive_chat",
    "authenticate",
    "cancel_generation",
    "clear_token",
    "create_chat",
    "extract_companion",
    "fetch_avatar",
    "fetch_companion_lorebooks",
    "fetch_lorebook",
    "find_echo_config",
    "get_provider_config",
    "has_token",
    "is_saucepan_url",
    "leak_definition",
    "leak_definition_via_echo",
    "list_provider_configs",
    "load_token",
    "login",
    "parse_companion_id",
    "resolve_provider_config",
    "set_provider_prompt",
    "set_token",
    "set_trace_level",
    "search_companions",
    "token_expiry",
    "update_provider_config",
    "use_token",
]
