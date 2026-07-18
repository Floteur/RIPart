"""Generic card-file extraction: rip any Tavern card URL into a ``result`` dict.

Downloads the card file (PNG / ``.charx`` / JSON, via an optional host adapter),
extracts the embedded card, and normalises it through
:func:`ripart.common.tavern.card_to_result`. For a PNG card the downloaded image
is reused as the portrait, so no second request is needed.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from ...common.tavern import card_to_result
from .client import (
    TavernCardError,
    card_id_from_url,
    download,
    extract_card_bytes,
    resolve_card_url,
)


def _noop(_message: str) -> None:
    pass


def extract_card(
    url: str,
    *,
    log: Callable[[str], None] = _noop,
) -> dict[str, Any]:
    """Rip a Tavern card file at ``url`` into a RIPart ``result`` dict.

    ``url`` may be a direct card-file URL (``.png``/``.charx``/``.json``) or a
    site URL a host adapter recognises (e.g. a ``character-tavern.com/character/``
    page). No login is required — these are open, publicly downloadable cards.
    """
    resolved = resolve_card_url(url)
    if resolved != url:
        log(f"resolved to card file: {resolved}")
    log("downloading card …")
    data, content_type = download(resolved)
    card, kind = extract_card_bytes(data, content_type)
    log(f"parsed {kind} card")

    # For a PNG card the file itself is the portrait; reuse it as the avatar.
    avatar = ""
    if kind == "png":
        avatar = "data:image/png;base64," + base64.b64encode(data).decode("ascii")

    return card_to_result(
        card,
        source_url=url,
        character_id=card_id_from_url(resolved),
        definition_source=f"tavern-{kind}",
        avatar_base64=avatar,
        extra_diagnostics={"cardFileUrl": resolved, "cardKind": kind},
    )


__all__ = ["TavernCardError", "extract_card"]
