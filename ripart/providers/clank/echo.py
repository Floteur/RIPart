"""Pull the character definition out of a clank echoed prompt.

clank's ``developer`` message opens with ``You are the following character:`` and
the generic RP rules begin at a boilerplate marker; everything between is the
character body, with a ``## DIALOGUE EXAMPLES`` section for example dialogue. The
generic echo-body location lives in :mod:`ripart.common.echo`; the clank-specific
*layout* parsing lives here.
"""

from __future__ import annotations

import re
from typing import Any

from ...common.echo import find_echo_body_in_messages
from ...common.echo import first_greeting as _first_greeting
from ...common.echo import first_system_message as _first_system_message

_CHAR_PREFIX = re.compile(r"^\s*You are the following character:\s*", re.I)
_BOILERPLATE_MARK = re.compile(
    r"\n\s*Your job is to stay fully in character at all times\.", re.I
)
_DLG_HEADER = re.compile(r"#+\s*DIALOGUE EXAMPLES", re.I)
_DLG_END = re.compile(r"End of dialogue examples", re.I)
_PLACEHOLDER_LINE = re.compile(r"\{\{user\}\}|\{\{char\}\}")


def _norm_ws(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def find_echo_body(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the newest echoed request body found in the chat messages."""
    return find_echo_body_in_messages(messages)


def _system_message(body: dict[str, Any]) -> str:
    return _first_system_message(body)


def _greeting_message(body: dict[str, Any]) -> str:
    """First assistant turn in the echoed prompt = the character's greeting."""
    return _first_greeting(body)


def split_definition(system_prompt: str) -> dict[str, str]:
    """Split a clank system prompt into definition / example / boilerplate.

    * ``definition``  — the character body (description, appearance, personality,
      habits, speaking style): between ``You are the following character:`` and
      the generic-rules boilerplate marker.
    * ``example``     — the ``{{user}}``/``{{char}}`` lines under the
      ``## DIALOGUE EXAMPLES`` section.
    * ``boilerplate`` — the generic clank RP/formatting/NSFW rules and persona
      injection (identical across characters); kept for creator notes.
    """
    text = system_prompt or ""
    mark = _BOILERPLATE_MARK.search(text)
    if mark:
        definition = _CHAR_PREFIX.sub("", text[: mark.start()]).strip()
        boilerplate = text[mark.start() :].strip()
    else:
        definition = _CHAR_PREFIX.sub("", text).strip()
        boilerplate = ""

    example = ""
    header = _DLG_HEADER.search(text)
    if header:
        section = text[header.end() :]
        end = _DLG_END.search(section)
        if end:
            section = section[: end.start()]
        lines = [ln for ln in section.splitlines() if _PLACEHOLDER_LINE.search(ln)]
        example = "\n".join(lines).strip()

    return {"definition": definition, "example": example, "boilerplate": boilerplate}


def _system_message_raw(parsed: dict[str, str] | None) -> str:
    """Reconstruct a readable raw dump for the sidecar (.leak.txt)."""
    if not parsed:
        return ""
    parts = [parsed["definition"]]
    if parsed.get("example"):
        parts.append("## DIALOGUE EXAMPLES\n" + parsed["example"])
    return "\n\n".join(p for p in parts if p)
