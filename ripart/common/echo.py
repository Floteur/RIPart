"""Echo-proxy leak parsing, shared by every provider.

The leak technique is the same across platforms: point a chat/companion's custom
LLM provider at an OpenAI-compatible worker that echoes the request body back as
the assistant reply. That body's system/developer message is the fully-assembled
prompt (character definition + injected lorebook), verbatim; the assistant turns
are the greetings. These helpers parse that echoed body — the *transport* differs
per provider (how the echo is triggered and retrieved), the *parsing* does not.
"""

from __future__ import annotations

import json
from typing import Any

from .text import strip_code_fence

# An OpenAI-compatible worker that echoes the request body back as the assistant
# message. Point a provider's custom ``provider_url``/base URL at it and the
# echoed system prompt is the character definition, verbatim. Overridable.
DEFAULT_ECHO_BASE_URL = "https://echollm.ecorsiste.workers.dev/v1"
ECHO_MODEL = "echo"

# Lorebook/memory entries inject as *system* messages, so diffs and definition
# extraction look only at these roles (never the user/assistant conversation).
SYSTEM_ROLES = ("developer", "system")


def find_echo_body_in_text(text: str) -> dict[str, Any] | None:
    """Parse an echoed OpenAI request body (``{"messages": [...]}``) out of a blob.

    The reply may be that JSON directly, or fenced / prefixed with prose; we
    locate the outermost object that carries a ``messages`` array.
    """
    if not isinstance(text, str) or '"messages"' not in text:
        return None
    stripped = strip_code_fence(text).strip()
    candidates = [stripped]
    start = stripped.find("{")
    if start > 0:
        candidates.append(stripped[start:])
    for cand in candidates:
        try:
            body = json.loads(cand)
        except ValueError:
            continue
        if isinstance(body, dict) and isinstance(body.get("messages"), list):
            return body
    return None


def find_echo_body_in_messages(
    messages: list[dict[str, Any]], roles: tuple[str, ...] | None = None
) -> dict[str, Any] | None:
    """Return the newest echoed request body found in a list of chat messages.

    Scans newest-first. ``roles`` optionally restricts which message roles are
    considered (e.g. only the assistant/companion turn holds the echo).
    """
    for msg in reversed(messages or []):
        if not isinstance(msg, dict):
            continue
        if roles is not None and msg.get("role") not in roles:
            continue
        body = find_echo_body_in_text(str(msg.get("content") or ""))
        if body is not None:
            return body
    return None


def system_text(body: dict[str, Any]) -> str:
    """Concatenate *all* system/developer message contents (verbatim prompt)."""
    parts: list[str] = []
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") in SYSTEM_ROLES:
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(content.strip())
    return "\n\n".join(parts).strip()


def first_system_message(body: dict[str, Any]) -> str:
    """The first system/developer message content (a single-block prompt)."""
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") in SYSTEM_ROLES:
            return str(msg.get("content") or "")
    return ""


def greetings(body: dict[str, Any]) -> list[str]:
    """All assistant turns in the echoed prompt = the character's greeting(s)."""
    out: list[str] = []
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                out.append(content.strip())
    return out


def first_greeting(body: dict[str, Any]) -> str:
    """The first assistant turn = the character's greeting."""
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return str(msg.get("content") or "")
    return ""


def role_texts(
    body: dict[str, Any] | None, roles: tuple[str, ...] | None = None
) -> list[str]:
    """Message contents whose role is in ``roles`` (all roles when ``None``)."""
    return [
        str(m.get("content") or "")
        for m in (body or {}).get("messages", [])
        if isinstance(m, dict) and (roles is None or m.get("role") in roles)
    ]
