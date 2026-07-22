"""Top-level chub.ai extraction: build a RIPart ``result`` dict from a URL.

chub is *open* — a public character's definition is served in full, so no login
and no leak trick are needed. The API node's structured ``definition`` is the
primary source (richest metadata + embedded lorebook); if a node omits it
(unlisted/removed), the downloadable card PNG is parsed as a fallback. Both
funnel through :func:`ripart.common.tavern.card_to_result`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...common.tavern import card_to_result, read_card_png
from .client import (
    ChubError,
    character_url,
    fetch_avatar,
    fetch_card_png,
    fetch_node,
    parse_full_path,
)

# chub tags a card with structural "topics" alongside real tags; drop the noise.
_STRUCTURAL_TOPICS = {"ROOT", "TAVERN"}


def _noop(_message: str) -> None:
    pass


def _definition_to_card(
    node: dict[str, Any], definition: dict[str, Any]
) -> dict[str, Any]:
    """Wrap chub's ``definition`` object as a Tavern V2 card dict."""
    topics = [
        str(t).strip()
        for t in (node.get("topics") or [])
        if str(t).strip() and str(t).strip().upper() not in _STRUCTURAL_TOPICS
    ]
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": definition.get("name") or node.get("name") or "",
            "description": definition.get("description") or "",
            "personality": definition.get("personality") or "",
            "scenario": definition.get("scenario") or "",
            "first_mes": definition.get("first_message") or "",
            "mes_example": definition.get("example_dialogs") or "",
            "alternate_greetings": definition.get("alternate_greetings") or [],
            "system_prompt": definition.get("system_prompt") or "",
            "post_history_instructions": definition.get("post_history_instructions")
            or "",
            "creator_notes": node.get("tagline") or "",
            "tags": topics,
            "character_book": definition.get("embedded_lorebook"),
        },
    }


def extract_character(
    url: str,
    *,
    log: Callable[[str], None] = _noop,
) -> dict[str, Any]:
    """Rip a chub.ai character into a RIPart ``result`` dict.

    ``url`` may be a ``chub.ai/characters/<creator>/<slug>`` URL (or a mirror
    host, or a bare ``<creator>/<slug>``). The result is shaped for
    :func:`ripart.common.cards.save_to_library`. No login is required.
    """
    full_path = parse_full_path(url)
    if not full_path:
        raise ChubError(
            "not a chub.ai character URL "
            "(expected chub.ai/characters/<creator>/<slug>)",
            400,
        )

    log(f"fetching node {full_path} …")
    node = fetch_node(full_path)
    character_id = str(node.get("id") or full_path.replace("/", "_"))
    creator = full_path.split("/")[0]
    is_nsfw = bool(node.get("nsfw_image"))
    extra_meta = {"fullPath": full_path}
    extra_diagnostics = {
        "fullPath": full_path,
        "starCount": node.get("starCount"),
        "tokenCount": node.get("nTokens"),
    }

    definition = node.get("definition")
    if isinstance(definition, dict) and definition.get("description"):
        log("public definition from API")
        card = _definition_to_card(node, definition)
        source = "chub-api"
    else:
        log("definition absent from API — trying card PNG")
        png = fetch_card_png(full_path)
        card = read_card_png(png) if png else None
        if card is None:
            raise ChubError(
                f"chub.ai exposed no definition for {full_path} "
                "(node has no definition and no readable card PNG)",
                404,
            )
        source = "chub-png"

    avatar = fetch_avatar(node.get("avatar_url") or node.get("max_res_url"))
    if not avatar:
        avatar = fetch_avatar(
            f"https://avatars.charhub.io/avatars/{full_path}/chara_card_v2.png"
        )

    return card_to_result(
        card,
        source_url=character_url(full_path),
        character_id=character_id,
        definition_source=source,
        character_name=node.get("name"),
        avatar_base64=avatar,
        creator_name=creator,
        is_nsfw=is_nsfw,
        extra_meta=extra_meta,
        extra_diagnostics=extra_diagnostics,
    )
