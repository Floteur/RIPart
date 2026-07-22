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
    values = (
        raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    )
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
        identity = str(stored.get("uid", uid))
        if identity in entries:
            suffix = 2
            while f"{identity}-{suffix}" in entries:
                suffix += 1
            identity = f"{identity}-{suffix}"
        entries[identity] = stored
    return entries


def _fingerprint(book: dict[str, Any], entries: dict[str, dict[str, Any]]) -> str:
    # Used only when the source did not expose a lorebook ID (e.g. an imported
    # Tavern card).  It is content-addressed and therefore deliberately does not
    # claim an external provider identity.
    # Identity must include every value that can change activation or injection.
    # Exclude provider bookkeeping, but retain exact entry objects and lorebook
    # settings so behaviorally different books never overwrite each other.
    material = json.dumps(
        {
            "title": norm(str(book.get("title") or book.get("name") or "")),
            "description": book.get("description"),
            "scan_depth": book.get("scan_depth", book.get("scanDepth")),
            "token_budget": book.get("token_budget", book.get("tokenBudget")),
            "recursive_scanning": book.get(
                "recursive_scanning", book.get("recursiveScanning")
            ),
            "extensions": book.get("extensions"),
            "entries": entries,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _safe_component(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value)


def _record_path(library_dir: Path, source: str, identity: str) -> Path:
    safe_source = _safe_component(source)
    safe_identity = _safe_component(identity)
    return library_dir / "lorebooks" / safe_source / f"{safe_identity}.json"


def _evidence_path(library_dir: Path, source: str) -> Path:
    return library_dir / "lorebooks" / _safe_component(source) / "evidence.json"


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


def _referenced_characters(book: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize a provider lorebook's character attachment index."""
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in book.get("referencedCharacters") or []:
        if not isinstance(item, dict):
            continue
        character_id = str(item.get("id") or "").strip()
        if not character_id or character_id in seen:
            continue
        seen.add(character_id)
        ref = {"id": character_id}
        for field in ("name", "url", "creator"):
            value = str(item.get(field) or "").strip()
            if value:
                ref[field] = value
        refs.append(ref)
    return refs


def _merge_referenced_characters(
    existing: Any, current: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Merge an attachment index without losing characters from an older crawl."""
    merged: list[dict[str, str]] = []
    positions: dict[str, int] = {}
    for item in [*(existing if isinstance(existing, list) else []), *current]:
        if not isinstance(item, dict):
            continue
        character_id = str(item.get("id") or "").strip()
        if not character_id:
            continue
        normalized = {"id": character_id}
        for field in ("name", "url", "creator"):
            value = str(item.get(field) or "").strip()
            if value:
                normalized[field] = value
        if character_id in positions:
            merged[positions[character_id]] = normalized
        else:
            positions[character_id] = len(merged)
            merged.append(normalized)
    return merged


def _write_unassigned_observations(
    library_dir: Path,
    source: str,
    character_id: str,
    result: dict[str, Any],
    now: str,
    attached_lorebook_ids: list[str],
    attribution_lorebook_ids: list[str] | None = None,
) -> str | None:
    """Persist private blocks and cross-character attribution evidence.

    A single capture with several private books cannot identify an owner.  When
    the exact same recovered block appears on another character, however, the
    intersection of their attached lorebook IDs is evidence.  Promote only a
    one-ID intersection; all other observations remain available for later
    captures rather than being guessed or discarded.
    """
    entries = _private_entries(result)
    if not entries or not character_id:
        return None
    if not attached_lorebook_ids:
        # No attached lorebook means no book to attribute these blocks to: the
        # recovered text (Janitor persona/guideline residue) can never be
        # promoted, so recording it as lorebook evidence is meaningless.
        return None
    path = (
        library_dir
        / "lorebooks"
        / _safe_component(source)
        / "unassigned"
        / f"{_safe_component(character_id)}.json"
    )
    trigger_map = result.get("recoveredTriggers") or {}
    recovered_constant_keys = {
        norm(value) for value in result.get("recoveredConstants") or []
    }
    # Readable provider entries are removed before generateAlpha residue is
    # separated.  When one or more attached books are closed, only those books
    # can own the recovered blocks; retaining readable books as candidates
    # prevents an otherwise unambiguous private dump from ever being promoted.
    candidate_ids = sorted(
        set(attribution_lorebook_ids or attached_lorebook_ids)
    )
    evidence_file = _evidence_path(library_dir, source)
    try:
        evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        evidence = {}
    evidence_items = evidence.get("observations") if isinstance(evidence, dict) else {}
    evidence_items = evidence_items if isinstance(evidence_items, dict) else {}

    new_observations: list[dict[str, Any]] = []
    assigned: dict[str, list[dict[str, Any]]] = {}
    for content in entries:
        fingerprint = hashlib.sha256(norm(content).encode("utf-8")).hexdigest()
        item = evidence_items.get(fingerprint)
        item = item if isinstance(item, dict) else {}
        sightings = (
            item.get("sightings") if isinstance(item.get("sightings"), list) else []
        )
        sighting = {
            "characterId": character_id,
            "candidateLorebookIds": candidate_ids,
            "seenAt": now,
        }
        # Replace this character's prior sighting.  A newer extractor may have
        # stronger attachment visibility (for example, it can now distinguish
        # one closed book from one readable book), and stale broad candidates
        # must not permanently block attribution.
        sightings = [
            old
            for old in sightings
            if not (isinstance(old, dict) and old.get("characterId") == character_id)
        ]
        sightings.append(sighting)
        sets = [
            set(old.get("candidateLorebookIds") or [])
            for old in sightings
            if isinstance(old, dict) and old.get("candidateLorebookIds")
        ]
        candidates = sorted(set.intersection(*sets)) if sets else []
        status = "inferred" if len(candidates) == 1 else "unassigned"
        item = {
            "content": content,
            "contentFingerprint": fingerprint,
            "sightings": sightings,
            "candidateLorebookIds": candidates,
            "attribution": {"status": status, "candidates": candidates},
            "updatedAt": now,
        }
        evidence_items[fingerprint] = item
        triggers = (
            trigger_map.get(norm(content), []) if isinstance(trigger_map, dict) else []
        )
        observation = {
            "content": content,
            "contentFingerprint": fingerprint,
            "attribution": {"status": status, "candidates": candidates},
        }
        if triggers:
            observation["inferredTriggers"] = triggers
        always_active = norm(content) in recovered_constant_keys
        if always_active:
            observation["alwaysActive"] = True
        new_observations.append(observation)
        if status == "inferred":
            assigned.setdefault(candidates[0], []).append(observation)

    write_json(
        evidence_file,
        {
            "schemaVersion": 1,
            "source": source,
            "updatedAt": now,
            "observations": evidence_items,
        },
    )
    # Reconcile prior character captures too.  The evidence record is the
    # source of truth; per-character files are a convenient, inspectable view.
    attribution_by_fingerprint = {
        fingerprint: item["attribution"]
        for fingerprint, item in evidence_items.items()
        if isinstance(item, dict) and isinstance(item.get("attribution"), dict)
    }
    for observation_file in path.parent.glob("*.json"):
        try:
            prior_capture = json.loads(observation_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        prior_observations = (
            prior_capture.get("observations")
            if isinstance(prior_capture, dict)
            else None
        )
        if not isinstance(prior_observations, list):
            continue
        changed = False
        for observation in prior_observations:
            if not isinstance(observation, dict):
                continue
            attribution = attribution_by_fingerprint.get(
                str(observation.get("contentFingerprint") or "")
            )
            if attribution and observation.get("attribution") != attribution:
                observation["attribution"] = attribution
                changed = True
        if changed:
            prior_capture["updatedAt"] = now
            write_json(observation_file, prior_capture)
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    prior = existing.get("observations") if isinstance(existing, dict) else []
    prior = prior if isinstance(prior, list) else []
    observations: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for observation in [*prior, *new_observations]:
        if not isinstance(observation, dict):
            continue
        fingerprint = str(observation.get("contentFingerprint") or "")
        if not fingerprint:
            continue
        if fingerprint in positions:
            # Prefer the fresh evidence-backed record over a stale copy from a
            # previous extraction of this same character.
            observations[positions[fingerprint]] = observation
            continue
        positions[fingerprint] = len(observations)
        observations.append(observation)
    write_json(
        path,
        {
            "schemaVersion": 1,
            "source": source,
            "characterId": character_id,
            "firstSeenAt": existing.get("firstSeenAt", existing.get("capturedAt", now))
            if isinstance(existing, dict)
            else now,
            "updatedAt": now,
            "observations": observations,
        },
    )
    # Attach promoted observations to their reusable provider-lorebook record.
    for lorebook_id, items in assigned.items():
        record_file = _record_path(library_dir, source, lorebook_id)
        try:
            record = json.loads(record_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        recovered = record.get("recoveredObservations")
        recovered = recovered if isinstance(recovered, list) else []
        positions = {
            str(item.get("contentFingerprint") or ""): index
            for index, item in enumerate(recovered)
            if isinstance(item, dict) and item.get("contentFingerprint")
        }
        for item in items:
            fingerprint = item["contentFingerprint"]
            if fingerprint in positions:
                recovered[positions[fingerprint]] = item
            else:
                positions[fingerprint] = len(recovered)
                recovered.append(item)
        record["recoveredObservations"] = recovered
        recovered_entries: dict[str, dict[str, Any]] = {}
        for index, observation in enumerate(recovered):
            if not isinstance(observation, dict):
                continue
            content = str(observation.get("content") or "").strip()
            if not content:
                continue
            triggers = [
                str(value).strip()
                for value in observation.get("inferredTriggers") or []
                if str(value).strip()
            ]
            recovered_entries[str(index)] = {
                "uid": index,
                "content": content,
                "key": triggers,
                "constant": bool(observation.get("alwaysActive")),
                "disable": not (observation.get("alwaysActive") or triggers),
                "comment": "Recovered from Janitor generateAlpha",
                "extensions": {
                    "ripart": {
                        "recovered": True,
                        "activation": (
                            "always"
                            if observation.get("alwaysActive")
                            else "inferred-keys"
                            if triggers
                            else "unknown"
                        ),
                        "contentFingerprint": observation.get("contentFingerprint"),
                    }
                },
            }
        record["recoveredWorldInfo"] = {"entries": recovered_entries}
        record["recoveredEntryCount"] = len(recovered_entries)
        record["updatedAt"] = now
        write_json(record_file, record)
    return str(path)


def update_lorebook_library(
    library_dir: Path,
    character_id: str,
    result: dict[str, Any],
) -> list[str]:
    """Upsert attached lorebooks and return the written record paths.

    Lorebooks with inaccessible entries retain their provider ID and attachment
    history.  Private recovered blocks are reconciled only when repeated
    observations leave one compatible attached lorebook.
    """
    source = _source_name(str(result.get("url") or ""))
    now = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    written: list[str] = []
    attached_lorebook_ids: list[str] = []
    inaccessible_lorebook_ids: list[str] = []
    for book in result.get("publicLorebooks") or []:
        if not isinstance(book, dict):
            continue
        entries = _entries(book)
        title = str(book.get("title") or "").strip()
        source_id = str(book.get("id") or "").strip() or None
        # A private Janitor lorebook still has a stable script ID. Keep that
        # record even when its entries cannot be fetched, so recovered prompt
        # text from multiple characters can later be attributed to it.
        if source_id:
            attached_lorebook_ids.append(source_id)
            is_code_public = book.get("isCodePublic")
            if is_code_public is False or (
                is_code_public is None and not book.get("accessible")
            ):
                inaccessible_lorebook_ids.append(source_id)
        if not entries and not source_id:
            continue
        fingerprint = _fingerprint(book, entries)
        identity = source_id or f"content-{fingerprint}"
        path = _record_path(library_dir, source, identity)
        try:
            existing = (
                json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            )
        except (OSError, json.JSONDecodeError):
            existing = {}
        character_ids = (
            existing.get("characterIds") if isinstance(existing, dict) else []
        )
        if not isinstance(character_ids, list):
            character_ids = []
        character_ids = [str(value) for value in character_ids if str(value).strip()]
        if character_id and character_id not in character_ids:
            character_ids.append(character_id)
        referenced_characters = _merge_referenced_characters(
            existing.get("referencedCharacters") if isinstance(existing, dict) else [],
            _referenced_characters(book),
        )
        recovered_observations = (
            existing.get("recoveredObservations") if isinstance(existing, dict) else []
        )
        recovered_observations = (
            recovered_observations if isinstance(recovered_observations, list) else []
        )
        description = str(book.get("description") or "").strip()
        if not description and isinstance(existing, dict):
            description = str(existing.get("description") or "").strip()
        record = {
            "schemaVersion": 1,
            "source": source,
            "sourceLorebookId": source_id,
            "contentFingerprint": fingerprint,
            "title": title,
            "description": description,
            "worldInfo": {"entries": entries},
            "entryCount": len(entries),
            "accessible": bool(book.get("accessible")),
            "characterIds": character_ids,
            # ``characterIds`` is the subset extracted into this local library.
            # The provider's script response can list additional public users of
            # the lorebook; retain them as a regeneration queue without claiming
            # they have already been captured locally.
            "referencedCharacters": referenced_characters,
            "recoveredObservations": recovered_observations,
            "firstSeenAt": existing.get("firstSeenAt", now)
            if isinstance(existing, dict)
            else now,
            "updatedAt": now,
        }
        write_json(path, record)
        written.append(str(path))
    unassigned_path = _write_unassigned_observations(
        library_dir,
        source,
        character_id,
        result,
        now,
        attached_lorebook_ids,
        inaccessible_lorebook_ids or attached_lorebook_ids,
    )
    if unassigned_path:
        written.append(unassigned_path)
    return written
