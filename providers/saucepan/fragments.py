"""Fragment reassembly — a verbatim port of Saucepan's client scheme.

Definitions ship as a SHUFFLED list of text fragments padded with decoy
fragments; a naive join is garbled. Each real fragment carries a ``proof`` hash
that the decoys fail; reassembly validates the proof, orders the survivors by
``key ^ mask``, and concatenates. Ported so the output matches byte-for-byte.
"""

from __future__ import annotations

from typing import Any

_FNV_OFFSET = 2166136261
_FNV_PRIME = 16777619
_U32 = 0xFFFFFFFF


def _rotl(value: int, bits: int) -> int:
    value &= _U32
    return ((value << bits) | (value >> (32 - bits))) & _U32


def _fragment_hash(mask: int, derived_key: int, text: str) -> int:
    h = (_FNV_OFFSET ^ _rotl(mask, 7) ^ _rotl(derived_key, 13)) & _U32
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * _FNV_PRIME) & _U32
    return h & _U32


def assemble_fragments(content: dict[str, Any] | None) -> str:
    """Reassemble a ``{fragments, mask}`` content object, dropping decoys."""
    content = content or {}
    fragments = content.get("fragments")
    if not isinstance(fragments, list):
        return ""
    mask = int(content.get("mask") or 0) & _U32

    survivors: list[dict[str, Any]] = []
    for frag in fragments:
        if not isinstance(frag, dict) or not isinstance(frag.get("text"), str):
            continue
        derived_key = (int(frag.get("key") or 0) ^ mask) & _U32
        if _fragment_hash(mask, derived_key, frag["text"]) == (
            int(frag.get("proof") or 0) & _U32
        ):
            survivors.append(frag)

    survivors.sort(key=lambda f: (int(f.get("key") or 0) ^ mask) & _U32)
    return "".join(f["text"] for f in survivors)
