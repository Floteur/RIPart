"""Saucepan lorebook fetching and parsing.

Saucepan flattens each lorebook entry into markdown that opens with a metadata
block carrying the trigger keys and a summary, then the real lore. We lift the
keys/comment out of that block into real fields and drop those metadata lines
from the injected content, shaping the result like JanitorAI's public-lorebook
``worldInfo.entries`` so the shared card builder embeds them with real keys.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from .client import _get_json
from .fragments import assemble_fragments

_LORE_ACTIVATION_RE = re.compile(r"\*\*\s*Activation Keys?\s*:\*\*\s*(.+)", re.I)
_LORE_SECONDARY_RE = re.compile(r"\*\*\s*Secondary Keys?\s*:\*\*\s*(.+)", re.I)
_LORE_COMMENT_RE = re.compile(r"\*\*\s*Comment\s*:\*\*\s*(.+)", re.I)
# A whole metadata line (incl. its trailing newline) to remove from content.
_LORE_MARKER_LINE_RE = re.compile(
    r"(?im)^[ \t]*\*\*\s*(?:Activation Keys?|Secondary Keys?|Comment)\s*:\*\*[^\n]*\n?"
)


def _split_keys(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _clean_lore_text(text: str) -> str:
    # Mirrors kiwi's normalizeSaucepanLorebookText: drop <br>/\r, strip a leading
    # "#  >>marker<<" header line, and collapse runs of blank lines.
    cleaned = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    cleaned = cleaned.replace("\r", "")
    cleaned = re.sub(r"^#\s*>>.*?<<\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _lorebook_world_info(entries: list[Any]) -> dict[str, dict[str, Any]]:
    """Turn a Saucepan lorebook's ``[{title, text}]`` into worldInfo entries.

    Shaped like JanitorAI's public-lorebook ``worldInfo.entries`` so the existing
    card builder embeds them with real trigger keys. The
    ``**Activation/Secondary Keys**`` and ``**Comment**`` metadata lines are
    lifted into ``key``/``keysecondary``/``comment`` and stripped from the
    injected content so the lore text stays clean.
    """
    world: dict[str, dict[str, Any]] = {}
    uid = 0
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        activation = _LORE_ACTIVATION_RE.search(text)
        secondary = _LORE_SECONDARY_RE.search(text)
        comment_match = _LORE_COMMENT_RE.search(text)
        keys = _split_keys(activation.group(1)) if activation else []
        secondary_keys = _split_keys(secondary.group(1)) if secondary else []
        comment = (comment_match.group(1).strip() if comment_match else "") or str(
            entry.get("title") or ""
        ).strip()
        # Drop the metadata lines from the content (keys/comment now live in
        # their own fields); keep the heading and the actual lore.
        body = _clean_lore_text(_LORE_MARKER_LINE_RE.sub("", text))
        world[str(uid)] = {
            "uid": uid,
            "content": body or _clean_lore_text(text),
            "key": keys,
            "keysecondary": secondary_keys,
            "comment": comment,
            # Keyed entries fire selectively; keyless ones stay always-on.
            "constant": not keys,
            "disable": False,
        }
        uid += 1
    return world


def _fetch_chapter_fragments(lorebook_id: str) -> list[dict[str, Any]]:
    """Fallback for restricted lorebooks: reassemble each chapter's fragments.

    ``/v1/lorebooks/{id}`` serves chapter text as plaintext when the book is
    readable; when it is not hydrated, ``/v2/lorebooks/{id}/chapters`` still
    carries the (obfuscated) ``text_fragments`` per chapter.
    """
    lid = quote(str(lorebook_id), safe="")
    ok, _status, data = _get_json(f"/api/v2/lorebooks/{lid}/chapters", True)
    if not ok or not isinstance(data, dict):
        return []
    chapters = data.get("chapters")
    if not isinstance(chapters, list):
        return []
    out: list[dict[str, Any]] = []
    for position, meta in enumerate(chapters):
        # The list is metadata only; the fragments live on the per-chapter route.
        index = meta.get("index", position) if isinstance(meta, dict) else position
        title = (meta.get("title") if isinstance(meta, dict) else "") or ""
        cok, _cstatus, cdata = _get_json(
            f"/api/v2/lorebooks/{lid}/chapters/{index}", True
        )
        if not cok or not isinstance(cdata, dict):
            continue
        text = assemble_fragments(cdata.get("text_fragments"))
        if text.strip():
            out.append({"title": title or cdata.get("title") or "", "text": text})
    return out


def fetch_lorebook(lorebook_id: str) -> dict[str, Any] | None:
    """Fetch one lorebook as a ``{title, worldInfo}`` book (or None on failure)."""
    ok, _status, data = _get_json(
        f"/api/v1/lorebooks/{quote(str(lorebook_id), safe='')}", True
    )
    name = ""
    chapters: list[Any] = []
    if ok and isinstance(data, dict):
        name = str(data.get("name") or "")
        if isinstance(data.get("content"), list):
            chapters = data["content"]
    # Plaintext content absent (restricted book) - reassemble fragmented chapters.
    if not chapters:
        chapters = _fetch_chapter_fragments(lorebook_id)
    world = _lorebook_world_info(chapters)
    if not world:
        return None
    return {"title": name, "worldInfo": {"entries": world}}


def fetch_companion_lorebooks(companion_id: str) -> list[dict[str, Any]]:
    """Fetch every lorebook attached to a companion (best effort; skips failures)."""
    ok, _status, data = _get_json(
        f"/api/v2/companions/{quote(str(companion_id), safe='')}/lorebooks",
        True,
    )
    if not ok or not isinstance(data, dict):
        return []
    books: list[dict[str, Any]] = []
    for item in data.get("lorebooks") or []:
        lorebook_id = item.get("id") if isinstance(item, dict) else None
        if not lorebook_id:
            continue
        book = fetch_lorebook(lorebook_id)
        if book:
            books.append(book)
    return books
