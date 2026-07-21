"""clank generation seam, auto-leak, and keyword-triggered lorebook dump.

The message-send / generation (:func:`trigger_generation`) IS wired — it posts to
clank's Vercel-AI-SDK endpoint ``/api/chat``. What is NOT wired is the
*provider-set* mutation (:func:`set_chat_llm_provider`), so ``--leak``
auto-configuration of a fresh chat still needs that captured from the browser;
``_MUTATIONS_WIRED`` gates the provider restore step.

A character's lorebook entries inject only when their trigger keywords appear in
recent messages, so :func:`dump_lorebook` fires the card's own text as bait and
diffs each expanded echo against the base prompt to recover the entries.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from ...common.echo import SYSTEM_ROLES, role_texts
from ...common.text import split_text_chunks
from .client import CLANK_ORIGIN, ClankError, _headers, _http
from .echo import _norm_ws, find_echo_body
from .trpc import get_chat_llm_settings, get_chat_messages

# See the module docstring: only the provider-set mutation remains unwired.
_MUTATIONS_WIRED = False

# Lorebook/memory entries inject as *system* messages (clank's own boilerplate:
# "Never reference system messages ... and lorebook injections"). So we diff only
# the system side — never the user/assistant conversation turns, which grow with
# history (and would otherwise flag prior echoes as huge false positives).
_SYSTEM_ROLES = SYSTEM_ROLES


def _noop(_message: str) -> None:
    pass


def set_chat_llm_provider(
    chat_id: str, base_url: str = "", model: str = "echo"
) -> dict[str, Any]:
    """Point the chat's custom LLM provider at ``base_url`` (returns prior settings).

    NOT YET WIRED — see ``_MUTATIONS_WIRED``. Needs the captured provider-set
    request.
    """
    del chat_id, base_url, model  # intentionally unused until the mutation is wired
    raise ClankError(
        "auto-leak is not wired yet: the 'set custom LLM provider' request must be "
        "captured from the browser first. For now, configure the echo proxy on the "
        "chat by hand, send one message, then run `rip clank extract` (no --leak)."
    )


def _last_assistant_id(chat_id: str) -> str | None:
    """The id of the most recent assistant message in the chat (branch point)."""
    for msg in reversed(get_chat_messages(chat_id)):
        if isinstance(msg, dict) and msg.get("message_type") == "assistant" and msg.get("id"):
            return str(msg["id"])
    return None


def trigger_generation(
    chat_id: str,
    message: str = "hi",
    *,
    chosen_last_assistant_id: str | None = None,
    timeout: int = 120,
) -> None:
    """Send ``message`` as a user turn to trigger a generation (the echo).

    Posts to clank's Vercel-AI-SDK endpoint ``/api/chat``; the response is a UI
    message stream which we drain to completion (that's when the assistant reply
    — here, the echoed request body — is persisted). The chat must already have a
    provider configured (the echo proxy for a leak). Needs the CSRF cookie.
    """
    if chosen_last_assistant_id is None:
        chosen_last_assistant_id = _last_assistant_id(chat_id)
    body = {
        "message": {
            "id": str(uuid.uuid4()),
            "role": "user",
            "parts": [{"type": "text", "text": str(message)}],
        },
        "id": chat_id,
        "selected_persona_id": None,
        "chat_id": chat_id,
        "use_draft_snapshot": False,
        "chosen_last_assistant_id": chosen_last_assistant_id,
    }
    headers = _headers(referer=f"{CLANK_ORIGIN}/chat/{chat_id}", json_body=True)
    if _http.trace_level >= 2:
        print(f"[clank-http] POST /api/chat (chat {chat_id})", flush=True)
    if _http.trace_level >= 3:
        print(f"[clank-http]   body: {_http.trace_preview(body)}", flush=True)
    try:
        response = _http.client().post(
            "/api/chat", headers=headers, json=body, timeout=timeout
        )
    except httpx.HTTPError as exc:
        raise ClankError(f"network error sending message: {exc}") from exc
    if response.status_code in (401, 403):
        raise ClankError(
            "not authenticated for /api/chat — re-login with the CSRF token "
            "(`rip clank login --csrf-token …`)",
            response.status_code,
        )
    if response.is_error:
        raise ClankError(
            f"send failed: HTTP {response.status_code} {response.text[:200]}",
            response.status_code,
        )
    _ = response.text  # drain the stream so the assistant echo is persisted


def leak_chat_definition(
    chat_id: str,
    *,
    base_url: str,
    message: str = "hi",
    timeout: int = 60,
    log: Callable[[str], None] = _noop,
) -> dict[str, Any] | None:
    """Auto-leak: set the proxy, trigger a generation, poll for the echo, restore.

    Returns the parsed echo body, or None. Restores the original provider in a
    ``finally``. Requires the write procedures to be wired.
    """
    prior = get_chat_llm_settings(chat_id)
    log("pointing chat LLM provider at the echo proxy …")
    set_chat_llm_provider(chat_id, base_url=base_url)
    try:
        log(f"sending trigger message ({message!r}) …")
        trigger_generation(chat_id, message)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = find_echo_body(get_chat_messages(chat_id))
            if body is not None:
                log("echo received")
                return body
            time.sleep(2.0)
        return None
    finally:
        _restore_provider(chat_id, prior, log)


def _restore_provider(
    chat_id: str, prior: dict[str, Any], log: Callable[[str], None]
) -> None:
    # Best-effort restore of whatever provider was configured before the leak.
    try:
        provider = prior.get("saved_provider") if isinstance(prior, dict) else None
        if provider and _MUTATIONS_WIRED:
            set_chat_llm_provider(
                chat_id,
                base_url=str(provider.get("custom_base_url") or ""),
                model=str(provider.get("custom_model") or ""),
            )
            log("restored original LLM provider")
    except ClankError:
        log("! could not restore the original LLM provider")


def build_trigger_messages(
    description: str = "",
    scenario: str = "",
    first_mes: str = "",
    *,
    extra: list[str] | None = None,
    chunk_size: int = 1500,
    min_len: int = 40,
) -> list[str]:
    """Chunk the card's own text into messages that fire lorebook keys.

    A character's lorebook entries inject only when their trigger keywords appear
    in recent messages, so the richest bait is the card's own prose — the
    description, scenario, and greeting almost always contain the keywords the
    creator keyed their lorebook on. Returns deduped, substantial chunks.
    """
    candidates: list[str] = []
    for source in (first_mes, scenario, description, *(extra or [])):
        candidates.extend(split_text_chunks(str(source or ""), chunk_size, min_len))

    out: list[str] = []
    seen: set[str] = set()
    for message in candidates:
        text = message.strip()
        if len(text) < min_len:
            continue
        key = _norm_ws(text).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _echo_texts(
    body: dict[str, Any] | None, roles: tuple[str, ...] | None = None
) -> list[str]:
    return role_texts(body, roles)


def extract_injections(
    base_body: dict[str, Any] | None,
    echo_body: dict[str, Any] | None,
    trigger_text: str = "",
) -> list[str]:
    """Return system-side text blocks in ``echo_body`` absent from ``base_body``.

    Diffs only the developer/system messages (where clank injects lorebook and
    memory content), paragraph by paragraph, and keeps blocks not already in the
    base prompt and not the trigger itself — i.e. what the trigger caused clank
    to inject. Conversation turns are ignored (they accumulate chat history).
    Pure and side-effect free, so it is unit-testable without sending anything.
    """
    base_norm = _norm_ws("\n".join(_echo_texts(base_body, _SYSTEM_ROLES))).lower()
    trig_norm = _norm_ws(trigger_text).lower()
    blocks: list[str] = []
    seen: set[str] = set()
    for text in _echo_texts(echo_body, _SYSTEM_ROLES):
        for para in re.split(r"\n\s*\n", text):
            block = para.strip()
            if len(block) < 25:
                continue
            norm = _norm_ws(block).lower()
            if norm in base_norm:  # already part of the base prompt
                continue
            if trig_norm and (norm in trig_norm or trig_norm in norm):
                continue  # it's our own trigger message echoed back
            if norm in seen:
                continue
            seen.add(norm)
            blocks.append(block)
    return blocks


def dump_lorebook(
    chat_id: str,
    *,
    description: str = "",
    scenario: str = "",
    first_mes: str = "",
    base_body: dict[str, Any] | None = None,
    triggers: list[str] | None = None,
    sleep: float = 3.0,
    max_triggers: int | None = None,
    log: Callable[[str], None] = _noop,
) -> list[str]:
    """Recover a character's lorebook entries via keyword-triggered echoes.

    For each trigger message (built from the card text unless ``triggers`` is
    given) it sends the message, reads the freshly expanded echo, and diffs it
    against ``base_body`` to collect any injected blocks. Returns the deduped
    injected entries (empty if the character has no lorebook).

    Requires the send seam (:func:`trigger_generation`) to be wired; until then
    it raises with guidance. The pure diff core (:func:`extract_injections`) and
    trigger builder (:func:`build_trigger_messages`) work without it.
    """
    if base_body is None:
        base_body = find_echo_body(get_chat_messages(chat_id))
    if base_body is None:
        raise ClankError(
            "no base echo in this chat — set the echo proxy as the chat's LLM "
            "provider and send one message first",
            404,
        )
    if triggers is None:
        triggers = build_trigger_messages(description, scenario, first_mes)
    if max_triggers is not None:
        triggers = triggers[:max_triggers]

    entries: list[str] = []
    seen: set[str] = set()
    for i, trigger in enumerate(triggers, 1):
        log(f"lorebook trigger {i}/{len(triggers)} ({len(trigger)} chars) …")
        trigger_generation(chat_id, trigger)  # send seam — raises until wired
        time.sleep(sleep)
        body = find_echo_body(get_chat_messages(chat_id))
        for block in extract_injections(base_body, body, trigger):
            key = _norm_ws(block).lower()
            if key in seen:
                continue
            seen.add(key)
            entries.append(block)
            log(f"  + injected block ({len(block)} chars)")
    return entries
