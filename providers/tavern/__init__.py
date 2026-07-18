"""Generic Tavern card-file ripper — "rip any website any card".

Open sites publish characters as downloadable **card files**: a PNG with the
card embedded, a ``.charx`` V3 archive, or a raw JSON card. This provider rips
any such URL with no login, and a small host-adapter table maps friendly site
URLs (e.g. ``character-tavern.com/character/<path>``) to their card-file URL.
Everything funnels through :func:`ripart.common.tavern.card_to_result`.

Re-exports the public surface so callers can
``from ripart.providers import tavern``.
"""

from __future__ import annotations

from .client import (
    TavernCardError,
    card_id_from_url,
    is_card_url,
    resolve_card_url,
    set_trace_level,
)
from .extract import extract_card

__all__ = [
    "TavernCardError",
    "card_id_from_url",
    "extract_card",
    "is_card_url",
    "resolve_card_url",
    "set_trace_level",
]
