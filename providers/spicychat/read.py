"""spicychat.ai read procedures: character fetch + public Typesense search.

Two read surfaces:

* ``GET /v2/characters/<id>`` — the richest record. Always returns metadata plus
  the ``greeting`` and a ``lorebooks`` list (names + entry counts, but never the
  entry *contents*). When ``definition_visible`` is true it *also* returns the
  gated fields — ``persona`` (the definition), ``dialogue`` (example messages)
  and ``scenario``. When false, those keys are simply absent.
* The public Typesense cluster the web app uses for browse/search — a read-only
  scoped key, so this needs no login.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .client import (
    NDAPI_BASE,
    SPICYCHAT_ORIGIN,
    SPICYCHAT_UA,
    TYPESENSE_BASE,
    TYPESENSE_KEY,
    SpicyChatError,
    _headers,
    _http,
)

_RETRY_ATTEMPTS = 3

# Fields worth pulling back from a search hit (a subset of the full document).
_SEARCH_FIELDS = (
    "name,title,tags,creator_username,creator_user_id,character_id,avatar_url,"
    "avatar_is_nsfw,is_nsfw,definition_visible,has_lorebooks,token_count,"
    "num_messages,rating_score,type"
)


def get_character(character_id: str) -> dict[str, Any]:
    """Return one character's record from ``GET /v2/characters/<id>``.

    Raises :class:`SpicyChatError` on a missing character or an auth rejection
    (the guest id is required; without it the API answers 401).
    """
    response = _http.send(
        "GET",
        f"{NDAPI_BASE}/v2/characters/{character_id}",
        headers=_headers(),
        attempts=_RETRY_ATTEMPTS,
        retry_5xx=True,
        trace_label=f"v2/characters/{character_id}",
    )
    if response.status_code == 404:
        raise SpicyChatError(f"character not found: {character_id}", 404)
    if response.status_code in (401, 403):
        raise SpicyChatError(
            "spicychat.ai rejected the request (guest identity refused)",
            response.status_code,
        )
    if response.is_error:
        raise SpicyChatError(
            f"spicychat.ai returned {response.status_code} for character {character_id}",
            response.status_code,
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise SpicyChatError(
            f"spicychat.ai returned non-JSON ({response.status_code})"
        ) from exc
    if not isinstance(data, dict):
        raise SpicyChatError("unexpected character response shape")
    return data


def search_characters(
    query: str = "",
    *,
    limit: int = 30,
    page: int = 1,
    tags: list[str] | None = None,
    include_nsfw: bool = True,
) -> dict[str, Any]:
    """Search the public character index (Typesense ``multi_search``).

    ``query`` matches name/title/tags/creator (empty = browse all, ranked by
    recent activity). ``tags`` AND-filters by tag. Returns
    ``{"hits": [<document>, ...], "found": int}`` where each document carries the
    :data:`_SEARCH_FIELDS` subset — enough to list results and hand a
    ``character_id`` to :func:`get_character`.
    """
    filters = ["application_ids:spicychat"]
    if not include_nsfw:
        filters.append("is_nsfw:false")
    for tag in tags or []:
        filters.append(f"tags:={json.dumps(str(tag))}")
    payload = {
        "searches": [
            {
                "collection": "public_characters_alias",
                "q": query or "*",
                "query_by": "name,title,tags,creator_username,character_id,type",
                "include_fields": _SEARCH_FIELDS,
                "filter_by": " && ".join(filters),
                "sort_by": "_text_match(buckets: 3):desc,num_messages_24h:desc",
                "per_page": max(1, min(int(limit), 250)),
                "page": max(1, int(page)),
            }
        ]
    }
    url = f"{TYPESENSE_BASE}/multi_search?use_cache=true&x-typesense-api-key={TYPESENSE_KEY}"
    try:
        response = _http.client().post(
            url,
            headers={
                "User-Agent": SPICYCHAT_UA,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "text/plain",
                "Origin": SPICYCHAT_ORIGIN,
                "Referer": f"{SPICYCHAT_ORIGIN}/",
            },
            content=json.dumps(payload),
        )
    except httpx.HTTPError as exc:
        raise SpicyChatError(f"network error talking to spicychat.ai search: {exc}") from exc
    if response.is_error:
        raise SpicyChatError(
            f"spicychat.ai search returned {response.status_code}", response.status_code
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise SpicyChatError("spicychat.ai search returned non-JSON") from exc
    results = data.get("results") if isinstance(data, dict) else None
    first = results[0] if isinstance(results, list) and results else {}
    hits = first.get("hits") if isinstance(first, dict) else None
    documents = [
        h.get("document")
        for h in (hits or [])
        if isinstance(h, dict) and isinstance(h.get("document"), dict)
    ]
    return {"hits": documents, "found": int(first.get("found") or 0)}
