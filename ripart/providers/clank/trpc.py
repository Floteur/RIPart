"""clank.world tRPC transport and read procedures.

tRPC wire format::

    GET  /api/trpc/<proc>?batch=1&input=<urlenc {"0": <input>}>
      -> {"0": {"result": {"data": <data>}}}   (or {"0": {"error": ...}})

The pooled client, retry and wire tracing come from the shared
:class:`~ripart.common.http.HttpClient` (see :mod:`.client`); this module adds the
tRPC envelope and the read procedures built on it.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from .client import (
    CLANK_BASE,
    ClankError,
    _fetch_html,
    _headers,
    _http,
    _UUID_ANY,
)

_RETRY_ATTEMPTS = 3

FEED_SORTS = ("new", "trending")


def _unwrap_trpc(payload: Any) -> Any:
    """Return ``result.data`` from a single-proc tRPC batch response, or raise."""
    if not isinstance(payload, list) or not payload:
        raise ClankError("unexpected tRPC response shape")
    entry = payload[0]
    if isinstance(entry, dict) and entry.get("error"):
        err = entry["error"]
        message = None
        if isinstance(err, dict):
            j = err.get("json") if isinstance(err.get("json"), dict) else err
            message = (j or {}).get("message")
        raise ClankError(message or "tRPC error")
    return (
        (((entry or {}).get("result") or {}).get("data"))
        if isinstance(entry, dict)
        else None
    )


def trpc_query(proc: str, input: dict[str, Any] | None = None) -> Any:
    """GET a single tRPC procedure and return its ``result.data``.

    Retries transient network / 429 / 5xx failures (via the shared client).
    """
    query = quote(json.dumps({"0": input if input is not None else {}}), safe="")
    path = f"/api/trpc/{proc}?batch=1&input={query}"
    response = _http.send(
        "GET",
        path,
        headers=_headers(),
        attempts=_RETRY_ATTEMPTS,
        retry_5xx=True,
        trace_label=proc,
    )
    if response.status_code == 404:
        raise ClankError(f"tRPC procedure not found: {proc}", 404)
    if response.status_code in (401, 403):
        raise ClankError("not authenticated - run `rip clank login`", 401)
    try:
        data = response.json()
    except ValueError as exc:
        raise ClankError(
            f"clank.world returned non-JSON ({response.status_code})"
        ) from exc
    return _unwrap_trpc(data)


def trpc_mutation(proc: str, input: dict[str, Any] | None = None) -> Any:
    """POST a single tRPC mutation and return its ``result.data``.

    Non-idempotent (create/generate), so it is attempted once.
    """
    body = {"0": input if input is not None else {}}
    path = f"/api/trpc/{proc}?batch=1"
    response = _http.send(
        "POST",
        path,
        headers=_headers(json_body=True),
        json_body=body,
        attempts=1,
        trace_label=proc,
    )
    if response.status_code == 404:
        raise ClankError(f"tRPC procedure not found: {proc}", 404)
    if response.status_code in (401, 403):
        raise ClankError("not authenticated - run `rip clank login`", 401)
    try:
        data = response.json()
    except ValueError as exc:
        raise ClankError(
            f"clank.world returned non-JSON ({response.status_code})"
        ) from exc
    return _unwrap_trpc(data)


# --------------------------------------------------------------------------- #
# Read procedures
# --------------------------------------------------------------------------- #


def get_chat_history(limit: int = 100) -> list[dict[str, Any]]:
    """Return the viewer's chat list (``agent.get_user_chat_history_with_pagination``).

    Items: ``{chat_id, scene_id, agent_name, agent_image, last_message, ...}`` —
    ``last_message`` holds the most recent message (which, for a leaked chat, is
    the echoed request body).
    """
    data = trpc_query(
        "agent.get_user_chat_history_with_pagination",
        {"limit": max(1, int(limit)), "direction": "forward"},
    )
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def resolve_character_chat(slug: str) -> str | None:
    """Find the viewer's existing chat for a character page ``@<slug>``.

    The character page embeds the UUIDs of the viewer's active chat / scene for
    that character; we intersect those with the chat history to recover the
    ``chat_id``. Returns None if the viewer has no chat with this character.
    """
    html = _fetch_html(f"/@{slug}")
    page_ids = {m.group(0).lower() for m in _UUID_ANY.finditer(html)}
    if not page_ids:
        return None
    items = get_chat_history(limit=100)
    # Prefer a chat whose own id is on the page; fall back to a scene match.
    for it in items:
        if str(it.get("chat_id") or "").lower() in page_ids:
            return str(it["chat_id"])
    for it in items:
        if str(it.get("scene_id") or "").lower() in page_ids:
            return str(it["chat_id"])
    return None


def get_chat_info(chat_id: str) -> dict[str, Any]:
    data = trpc_query("agent.get_chat_info", {"chat_id": chat_id})
    return data if isinstance(data, dict) else {}


def get_chat_llm_settings(chat_id: str) -> dict[str, Any]:
    data = trpc_query("agent.get_chat_llm_settings", {"chat_id": chat_id})
    return data if isinstance(data, dict) else {}


def get_story(scene_id: str) -> dict[str, Any]:
    """Return a scene/story's public data (``agent.get_clank_story_by_id``).

    Includes ``prompt`` (the scenario), ``initial_message`` (greetings),
    ``tags``, ``characters``, ``visibility`` — richer than ``get_chat_info``'s
    embedded ``story_data``. Returns ``{}`` on any failure (non-fatal).
    """
    try:
        data = trpc_query("agent.get_clank_story_by_id", {"scene_id": scene_id})
    except ClankError:
        return {}
    return data if isinstance(data, dict) else {}


def list_lorebooks(agent_id: str | None = None) -> list[dict[str, Any]]:
    """Return the viewer's lorebooks (``lorebook.list``).

    NOTE: this lists the *viewer's own* lorebooks, not a character creator's —
    clank injects creator lorebook entries into the prompt on keyword triggers
    rather than exposing them via a read endpoint, so they surface only in an
    echo whose recent messages hit their triggers. Usually ``[]`` when ripping
    someone else's character.
    """
    try:
        data = trpc_query("lorebook.list", {"agent_id": agent_id} if agent_id else {})
    except ClankError:
        return []
    return data if isinstance(data, list) else []


def get_lorebook(lorebook_id: str) -> dict[str, Any]:
    """Return one lorebook and its entries (``lorebook.get``)."""
    try:
        data = trpc_query("lorebook.get", {"lorebook_id": lorebook_id})
    except ClankError:
        return {}
    return data if isinstance(data, dict) else {}


def get_chat_messages(chat_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return the chat's messages (oldest first), from the paginated endpoint."""
    data = trpc_query(
        "agent.get_chat_messages_paginated", {"chat_id": chat_id, "limit": limit}
    )
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def list_stories(
    *,
    sort: str = "new",
    limit: int = 20,
    cursor: str | None = None,
    tags: list[str] | None = None,
    include_nsfw: bool = True,
    use_stricter_nsfw: bool = False,
) -> dict[str, Any]:
    """Return one page of the public browse feed (``agent.get_all_clank_stories``).

    ``sort="new"`` is newest-first; ``"trending"`` is clank's ranked feed. Returns
    ``{"items": [...], "nextCursor": str | None, "hasMore": bool}``. Pass the
    returned ``nextCursor`` back as ``cursor`` for the next page. ``tags`` filters
    by tag (ignored for the personalised "for you" tab, which this does not use).

    Each item is a *story/scene*: ``{id, agent_id, agent_username, agent_name,
    title, prompt, image_url, initial_message, tags, is_nsfw, possible_minor,
    characters, total_chats, active_chat_id, created_at, ...}``. ``prompt`` is the
    public scenario blurb — the gated character definition is NOT here (use the
    echo leak for that).
    """
    if sort not in FEED_SORTS:
        raise ClankError(f"sort must be one of {FEED_SORTS}, got {sort!r}")
    payload: dict[str, Any] = {
        "limit": max(1, int(limit)),
        "sort_by": sort,
        "include_nsfw": bool(include_nsfw),
        "use_stricter_nsfw": bool(use_stricter_nsfw),
        "direction": "forward",
    }
    if cursor:
        payload["cursor"] = cursor
    if tags:
        payload["selected_tags"] = list(tags)
    data = trpc_query("agent.get_all_clank_stories", payload)
    if not isinstance(data, dict):
        return {"items": [], "nextCursor": None, "hasMore": False}
    return {
        "items": data.get("items") or [],
        "nextCursor": data.get("nextCursor"),
        "hasMore": bool(data.get("hasMore")),
    }


def iter_stories(
    *,
    sort: str = "new",
    limit: int | None = None,
    page_size: int = 20,
    tags: list[str] | None = None,
    include_nsfw: bool = True,
):
    """Yield feed items across pages (newest-first for ``sort="new"``).

    Stops after ``limit`` items (``None`` = until the feed is exhausted),
    following ``nextCursor`` between pages.
    """
    yielded = 0
    cursor: str | None = None
    while True:
        page = list_stories(
            sort=sort,
            limit=page_size,
            cursor=cursor,
            tags=tags,
            include_nsfw=include_nsfw,
        )
        for item in page["items"]:
            yield item
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        cursor = page["nextCursor"]
        if not page["hasMore"] or not cursor:
            return


def story_character_url(item: dict[str, Any]) -> str:
    """The character page URL for a feed item (``clank.world/@<agent_username>``)."""
    username = str(item.get("agent_username") or "").strip()
    return f"{CLANK_BASE}/@{username}" if username else CLANK_BASE
