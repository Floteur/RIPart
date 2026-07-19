"""Durable lorebook records shared by characters in the local library.

Character cards retain an embedded copy for Tavern compatibility.  This module
keeps the provider lorebook as a separate, reusable record so a book attached to
several characters is represented once and can accumulate later observations.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .text import norm, write_json


def _source_name(source_url: str) -> str:
    host = urlparse(source_url).netloc.lower()
    for name in ("janitor", "saucepan", "chub", "clank", "spicychat"):
        if name in host:
            return name
    return host.replace(".", "_") or "unknown"


def _entries(book: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = (book.get("worldInfo") or {}).get("entries") or {}
    values = raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    entries: dict[str, dict[str, Any]] = {}
    for uid, entry in enumerate(values):
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        # Keep every World Info option.  Keys alone do not define behaviour:
        # position, order, filters, recursion, probability, and vectorization
        # all affect whether and where an entry is injected.
        stored = json.loads(json.dumps(entry, ensure_ascii=False))
        stored["content"] = content
        entries[str(stored.get("uid", uid))] = stored
    return entries


def _fingerprint(title: str, entries: dict[str, dict[str, Any]]) -> str:
    # Used only when the source did not expose a lorebook ID (e.g. an imported
    # Tavern card).  It is content-addressed and therefore deliberately does not
    # claim an external provider identity.
    material = "\n".join(
        [norm(title)]
        + sorted(
            "|".join(
                (
                    norm(str(entry.get("content") or "")),
                    ",".join(sorted(norm(str(key)) for key in entry.get("key") or [])),
                    ",".join(
                        sorted(norm(str(key)) for key in entry.get("keysecondary") or [])
                    ),
                )
            )
            for entry in entries.values()
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _safe_component(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value)


def _record_path(library_dir: Path, source: str, identity: str) -> Path:
    safe_source = _safe_component(source)
    safe_identity = _safe_component(identity)
    return library_dir / "lorebooks" / safe_source / f"{safe_identity}.json"


def _private_entries(result: dict[str, Any]) -> list[str]:
    entries: list[str] = []
    seen: set[str] = set()
    for raw in result.get("entries") or []:
        content = (
            str(raw.get("content") or raw.get("text") or "")
            if isinstance(raw, dict)
            else str(raw or "")
        ).strip()
        key = norm(content)
        if not key or key in seen:
            continue
        seen.add(key)
        entries.append(content)
    return entries


def _write_unassigned_observations(
    library_dir: Path,
    source: str,
    character_id: str,
    result: dict[str, Any],
    now: str,
) -> str | None:
    """Persist private blocks without guessing which attached book owns them."""
    entries = _private_entries(result)
    if not entries or not character_id:
        return None
    path = (
        library_dir
        / "lorebooks"
        / _safe_component(source)
        / "unassigned"
        / f"{_safe_component(character_id)}.json"
    )
    observations = [
        {
            "content": content,
            "contentFingerprint": hashlib.sha256(norm(content).encode("utf-8")).hexdigest(),
            "attribution": {"status": "unassigned", "candidates": []},
        }
        for content in entries
    ]
    write_json(
        path,
        {
            "schemaVersion": 1,
            "source": source,
            "characterId": character_id,
            "capturedAt": now,
            "observations": observations,
        },
    )
    return str(path)


def update_lorebook_library(
    library_dir: Path,
    character_id: str,
    result: dict[str, Any],
) -> list[str]:
    """Upsert accessible lorebooks and return the written record paths.

    Private recovered blocks are written as unassigned observations: an
    extraction does not reveal which of multiple private books produced them.
    A future evidence-based reconciliation step can assign them safely.
    """
    source = _source_name(str(result.get("url") or ""))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    written: list[str] = []
    for book in result.get("publicLorebooks") or []:
        if not isinstance(book, dict):
            continue
        entries = _entries(book)
        if not entries:
            continue
        title = str(book.get("title") or "").strip()
        source_id = str(book.get("id") or "").strip() or None
        fingerprint = _fingerprint(title, entries)
        identity = source_id or f"content-{fingerprint}"
        path = _record_path(library_dir, source, identity)
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
        character_ids = existing.get("characterIds") if isinstance(existing, dict) else []
        if not isinstance(character_ids, list):
            character_ids = []
        character_ids = [str(value) for value in character_ids if str(value).strip()]
        if character_id and character_id not in character_ids:
            character_ids.append(character_id)
        record = {
            "schemaVersion": 1,
            "source": source,
            "sourceLorebookId": source_id,
            "contentFingerprint": fingerprint,
            "title": title,
            "worldInfo": {"entries": entries},
            "entryCount": len(entries),
            "characterIds": character_ids,
            "firstSeenAt": existing.get("firstSeenAt", now) if isinstance(existing, dict) else now,
            "updatedAt": now,
        }
        write_json(path, record)
        written.append(str(path))
    unassigned_path = _write_unassigned_observations(
        library_dir, source, character_id, result, now
    )
    if unassigned_path:
        written.append(unassigned_path)
    return written
