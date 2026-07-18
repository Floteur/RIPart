"""Provider-agnostic text & small file utilities.

Pure and side-effect-free (except :func:`write_json`), so they can be unit
tested without a browser or network.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any


def norm(value: str) -> str:
    """Whitespace-collapse and lowercase — the canonical form for dedup/compare."""
    return re.sub(r"\s+", " ", value or "").strip().lower()


def safe_name(name: str, fallback: str) -> str:
    clean = re.sub(r"[^\w.\- ]+", "_", (name or "").strip() or fallback)
    return clean[:80].strip() or fallback


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def html_to_text(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.I)
    text = re.sub(r"</(p|div|li|h\d)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def strip_code_fence(text: str) -> str:
    """Remove one surrounding ``` fenced block (with an optional language tag)."""
    stripped = (text or "").strip()
    stripped = re.sub(r"^`{3,}[\w-]*\n", "", stripped)
    stripped = re.sub(r"\n`{3,}\s*$", "", stripped)
    return stripped.strip()


def split_text_chunks(text: str, max_len: int = 2500, min_len: int = 40) -> list[str]:
    """Split ``text`` into paragraph-aligned chunks no longer than ``max_len``.

    Keeps paragraphs together where possible; hard-splits any single paragraph
    that exceeds ``max_len``. Drops chunks shorter than ``min_len``.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        paragraphs = [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        sep_len = 2 if current else 0
        if current and current_len + sep_len + len(para) > max_len:
            chunk = "\n\n".join(current)
            if len(chunk) >= min_len:
                chunks.append(chunk)
            current = []
            current_len = 0
        if len(para) > max_len:
            if current:
                chunk = "\n\n".join(current)
                if len(chunk) >= min_len:
                    chunks.append(chunk)
                current = []
                current_len = 0
            for offset in range(0, len(para), max_len):
                piece = para[offset : offset + max_len].strip()
                if len(piece) >= min_len:
                    chunks.append(piece)
            continue
        current.append(para)
        current_len += sep_len + len(para)
    if current:
        chunk = "\n\n".join(current)
        if len(chunk) >= min_len:
            chunks.append(chunk)
    return chunks
