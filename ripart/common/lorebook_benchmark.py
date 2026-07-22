"""Fidelity benchmarks for reconstructed lorebooks.

The readable and reconstructed books attached to a character are not guaranteed
to be byte-identical editions.  These metrics therefore form a silver standard:
they quantify content coverage and structural similarity without claiming the
public book is the hidden book's exact source text.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from stopwordsiso import stopwords

_STOPWORDS = stopwords(["en"])
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'-]{2,}")


def _entry_map(record: dict[str, Any], field: str) -> dict[str, dict[str, Any]]:
    raw = (record.get(field) or {}).get("entries") or {}
    values = raw.items() if isinstance(raw, dict) else enumerate(raw)
    return {
        str(uid): entry
        for uid, entry in values
        if isinstance(entry, dict) and str(entry.get("content") or "").strip()
    }


def reconstructed_record_from_capture(result: dict[str, Any]) -> dict[str, Any]:
    """Build benchmark-only World Info from a blind generateAlpha capture."""
    trigger_map = result.get("recoveredTriggers") or {}
    constant_keys = {
        _normalize(str(value)) for value in result.get("recoveredConstants") or []
    }
    entries: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(result.get("entries") or []):
        content = str(
            raw.get("content") or raw.get("text") or ""
            if isinstance(raw, dict)
            else raw or ""
        ).strip()
        if not content:
            continue
        key = _normalize(content)
        entries[str(index)] = {
            "uid": index,
            "content": content,
            "key": list(trigger_map.get(key) or []),
            "constant": key in constant_keys,
        }
    return {
        "sourceLorebookId": "blind-capture",
        "title": "Blind generateAlpha capture",
        "recoveredWorldInfo": {"entries": entries},
        "recoveryRuns": [],
    }


def merge_blind_captures(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge captures from every character attached to the same lorebook.

    Entry bodies and inferred keys are unioned.  Baseline-active entries are
    treated as constant only when they appeared in every character's baseline;
    this removes character-card-triggered entries from the constant estimate.
    """
    if not results:
        raise ValueError("cannot merge zero blind captures")
    entries: list[str] = []
    content_by_key: dict[str, str] = {}
    trigger_map: dict[str, list[str]] = {}
    constant_sets: list[set[str]] = []
    for result in results:
        constant_sets.append(
            {_normalize(str(value)) for value in result.get("recoveredConstants") or []}
        )
        for raw in result.get("entries") or []:
            content = str(
                raw.get("content") or raw.get("text") or ""
                if isinstance(raw, dict)
                else raw or ""
            ).strip()
            key = _normalize(content)
            if not key:
                continue
            if key not in content_by_key:
                content_by_key[key] = content
                entries.append(content)
        for content_key, triggers in (result.get("recoveredTriggers") or {}).items():
            key = _normalize(str(content_key))
            values = trigger_map.setdefault(key, [])
            for trigger in triggers or []:
                trigger = str(trigger).strip()
                if trigger and trigger.casefold() not in {v.casefold() for v in values}:
                    values.append(trigger)
    constants = set.intersection(*constant_sets) if constant_sets else set()
    return {
        "entries": entries,
        "recoveredTriggers": trigger_map,
        "recoveredConstants": [
            content_by_key[key] for key in content_by_key if key in constants
        ],
        "benchmarkReferenceLorebook": results[0].get("benchmarkReferenceLorebook"),
        "captureCharacterIds": [
            str(result.get("characterId") or "") for result in results
        ],
        "captureRuns": [
            {
                "characterId": result.get("characterId"),
                "characterName": result.get("characterName"),
                "entries": len(result.get("entries") or []),
                "baselineActiveEntries": len(result.get("recoveredConstants") or []),
                "diagnostics": result.get("diagnostics"),
            }
            for result in results
        ],
    }
def injectable_reference_record(
    reference: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Return a benchmark reference containing only server-injectable entries."""
    record = copy.deepcopy(reference)
    record["sourceLorebookId"] = str(
        record.get("sourceLorebookId") or record.get("id") or ""
    )
    entries = _entry_map(record, "worldInfo")
    injectable = {
        uid: entry
        for uid, entry in entries.items()
        if entry.get("enabled") is not False and entry.get("disable") is not True
    }
    record["worldInfo"] = {"entries": injectable}
    return record, len(entries) - len(injectable)


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall(str(text or "").lower())
        if token not in _STOPWORDS
    }


def _ngrams(text: str, size: int = 8) -> set[str]:
    normalized = _normalize(text)
    return {
        normalized[index : index + size]
        for index in range(max(0, len(normalized) - size + 1))
    }


def _prf(reference: set[str], recovered: set[str]) -> tuple[float, float, float]:
    overlap = len(reference & recovered)
    recall = overlap / max(1, len(reference))
    precision = overlap / max(1, len(recovered))
    f1 = (
        2 * recall * precision / (recall + precision)
        if recall + precision
        else 0.0
    )
    return recall, precision, f1


def entry_similarity(reference: str, recovered: str) -> dict[str, float]:
    """Score one entry pair using semantic tokens and verbatim character grams."""
    token_recall, token_precision, token_f1 = _prf(
        _tokens(reference), _tokens(recovered)
    )
    gram_recall, gram_precision, gram_f1 = _prf(
        _ngrams(reference), _ngrams(recovered)
    )
    return {
        "score": 0.75 * token_f1 + 0.25 * gram_f1,
        "tokenRecall": token_recall,
        "tokenPrecision": token_precision,
        "tokenF1": token_f1,
        "char8Recall": gram_recall,
        "char8Precision": gram_precision,
        "char8F1": gram_f1,
    }


def _optimal_alignment(scores: list[list[float]]) -> list[tuple[int, int]]:
    """Return a deterministic maximum-weight one-to-one entry alignment."""
    if not scores or not scores[0]:
        return []
    row_count, column_count = len(scores), len(scores[0])
    transposed = row_count > column_count
    matrix = (
        [[scores[row][column] for row in range(row_count)] for column in range(column_count)]
        if transposed
        else scores
    )
    rows, columns = len(matrix), len(matrix[0])

    if columns <= 16:
        @lru_cache(maxsize=None)
        def solve(row: int, used: int) -> tuple[float, tuple[int, ...]]:
            if row == rows:
                return 0.0, ()
            best_score = -1.0
            best_columns: tuple[int, ...] = ()
            for column in range(columns):
                if used & (1 << column):
                    continue
                tail_score, tail_columns = solve(row + 1, used | (1 << column))
                candidate_score = matrix[row][column] + tail_score
                candidate_columns = (column, *tail_columns)
                if candidate_score > best_score or (
                    candidate_score == best_score
                    and candidate_columns < best_columns
                ):
                    best_score = candidate_score
                    best_columns = candidate_columns
            return best_score, best_columns

        _score, selected = solve(0, 0)
        pairs = list(enumerate(selected))
    else:
        # Large lorebooks avoid exponential assignment. Greedy is deterministic
        # and remains useful for trend comparison, while normal fixtures use the
        # exact solver above.
        available = {
            (matrix[row][column], row, column)
            for row in range(rows)
            for column in range(columns)
        }
        pairs = []
        used_rows: set[int] = set()
        used_columns: set[int] = set()
        for _score, row, column in sorted(available, reverse=True):
            if row in used_rows or column in used_columns:
                continue
            used_rows.add(row)
            used_columns.add(column)
            pairs.append((row, column))
            if len(pairs) == rows:
                break

    return [(column, row) for row, column in pairs] if transposed else pairs


def benchmark_lorebooks(
    reference: dict[str, Any], reconstructed: dict[str, Any]
) -> dict[str, Any]:
    """Compare a readable provider record with a reconstructed provider record."""
    reference_entries = _entry_map(reference, "worldInfo")
    recovered_entries = _entry_map(reconstructed, "recoveredWorldInfo")
    reference_items = list(reference_entries.items())
    recovered_items = list(recovered_entries.items())
    pair_metrics = [
        [
            entry_similarity(ref[1]["content"], rec[1]["content"])
            for rec in recovered_items
        ]
        for ref in reference_items
    ]
    alignment = _optimal_alignment(
        [[metrics["score"] for metrics in row] for row in pair_metrics]
    )
    alignment_threshold = 0.10
    matches = []
    aligned_reference_uids: set[str] = set()
    aligned_recovered_uids: set[str] = set()
    for reference_index, recovered_index in alignment:
        reference_uid, reference_entry = reference_items[reference_index]
        recovered_uid, recovered_entry = recovered_items[recovered_index]
        metrics = pair_metrics[reference_index][recovered_index]
        if metrics["score"] < alignment_threshold:
            continue
        aligned_reference_uids.add(reference_uid)
        aligned_recovered_uids.add(recovered_uid)
        matches.append(
            {
                "referenceUid": reference_uid,
                "recoveredUid": recovered_uid,
                **metrics,
                "referencePreview": " ".join(
                    str(reference_entry["content"]).split()
                )[:160],
                "recoveredPreview": " ".join(
                    str(recovered_entry["content"]).split()
                )[:160],
            }
        )
    matches.sort(key=lambda item: item["score"], reverse=True)

    reference_text = "\n\n".join(
        str(entry["content"]) for entry in reference_entries.values()
    )
    recovered_text = "\n\n".join(
        str(entry["content"]) for entry in recovered_entries.values()
    )
    token_recall, token_precision, token_f1 = _prf(
        _tokens(reference_text), _tokens(recovered_text)
    )
    gram_recall, gram_precision, gram_f1 = _prf(
        _ngrams(reference_text), _ngrams(recovered_text)
    )
    best_reference_scores = [
        max((item["score"] for item in row), default=0.0) for row in pair_metrics
    ]
    best_recovered_scores = [
        max(
            (pair_metrics[row][column]["score"] for row in range(len(reference_items))),
            default=0.0,
        )
        for column in range(len(recovered_items))
    ]
    aggregate_reference_token_coverage = [
        _prf(_tokens(str(entry["content"])), _tokens(recovered_text))[0]
        for entry in reference_entries.values()
    ]
    aggregate_reference_gram_coverage = [
        _prf(_ngrams(str(entry["content"])), _ngrams(recovered_text))[0]
        for entry in reference_entries.values()
    ]
    recovered_reference_precision = [
        max(
            (
                pair_metrics[row][column]["char8Precision"]
                for row in range(len(reference_items))
            ),
            default=0.0,
        )
        for column in range(len(recovered_items))
    ]
    reference_attribution_threshold = 0.80
    reference_attributed_entries = sum(
        score >= reference_attribution_threshold
        for score in recovered_reference_precision
    )
    aligned_scores = [item["score"] for item in matches]
    reference_trigger_keys = {
        _normalize(str(value))
        for entry in reference_entries.values()
        for value in entry.get("key") or []
        if _normalize(str(value))
    }
    recovered_trigger_keys = {
        _normalize(str(value))
        for entry in recovered_entries.values()
        for value in entry.get("key") or []
        if _normalize(str(value))
    }
    constants_compared = 0
    constants_correct = 0
    constant_true_positives = 0
    for match in matches:
        reference_entry = reference_entries[match["referenceUid"]]
        recovered_entry = recovered_entries[match["recoveredUid"]]
        constants_compared += 1
        constants_correct += (
            bool(reference_entry.get("constant"))
            == bool(recovered_entry.get("constant"))
        )
        constant_true_positives += bool(reference_entry.get("constant")) and bool(
            recovered_entry.get("constant")
        )
    trigger_recall, trigger_precision, trigger_f1 = _prf(
        reference_trigger_keys, recovered_trigger_keys
    )
    reference_constants = sum(
        bool(entry.get("constant")) for entry in reference_entries.values()
    )
    recovered_constants = sum(
        bool(entry.get("constant")) for entry in recovered_entries.values()
    )

    def mean(values: list[float]) -> float:
        return sum(values) / max(1, len(values))

    metrics = {
        "referenceEntries": len(reference_entries),
        "recoveredEntries": len(recovered_entries),
        "entryCountRatio": len(recovered_entries) / max(1, len(reference_entries)),
        "referenceChars": len(reference_text),
        "recoveredChars": len(recovered_text),
        "charCountRatio": len(recovered_text) / max(1, len(reference_text)),
        "tokenRecall": token_recall,
        "tokenPrecision": token_precision,
        "tokenF1": token_f1,
        "char8Recall": gram_recall,
        "char8Precision": gram_precision,
        "char8F1": gram_f1,
        "meanBestReferenceScore": mean(best_reference_scores),
        "meanBestRecoveredScore": mean(best_recovered_scores),
        "meanSplitAwareTokenCoverage": mean(aggregate_reference_token_coverage),
        "meanSplitAwareChar8Coverage": mean(aggregate_reference_gram_coverage),
        "splitAwareReferenceCoverageAt50": mean(
            [score >= 0.50 for score in aggregate_reference_gram_coverage]
        ),
        "splitAwareReferenceCoverageAt75": mean(
            [score >= 0.75 for score in aggregate_reference_gram_coverage]
        ),
        "referenceAttributedRecoveredEntries": reference_attributed_entries,
        "foreignOrAmbiguousRecoveredEntries": len(recovered_entries)
        - reference_attributed_entries,
        "referenceAttributedEntryRatio": reference_attributed_entries
        / max(1, len(recovered_entries)),
        "alignedMeanScore": mean(aligned_scores),
        "referenceCoverageAt25": mean(
            [score >= 0.25 for score in best_reference_scores]
        ),
        "referenceCoverageAt50": mean(
            [score >= 0.50 for score in best_reference_scores]
        ),
        "referenceCoverageAt75": mean(
            [score >= 0.75 for score in best_reference_scores]
        ),
        "alwaysActiveRecoveredEntries": sum(
            bool(entry.get("constant")) for entry in recovered_entries.values()
        ),
        "keyedRecoveredEntries": sum(
            bool(entry.get("key")) for entry in recovered_entries.values()
        ),
        "referenceTriggerKeys": len(reference_trigger_keys),
        "recoveredTriggerKeys": len(recovered_trigger_keys),
        "triggerRecall": trigger_recall,
        "triggerPrecision": trigger_precision,
        "triggerF1": trigger_f1,
        "constantAccuracy": constants_correct / max(1, constants_compared),
        "referenceConstants": reference_constants,
        "recoveredConstants": recovered_constants,
        "constantRecall": constant_true_positives / max(1, reference_constants),
        "constantPrecision": constant_true_positives / max(1, recovered_constants),
        "alignedEntries": len(matches),
        "unmatchedReferenceEntries": len(reference_entries)
        - len(aligned_reference_uids),
        "unmatchedRecoveredEntries": len(recovered_entries)
        - len(aligned_recovered_uids),
    }
    return {
        "schemaVersion": 1,
        "benchmarkType": "lorebook-silver-standard",
        "caveat": (
            "The public and reconstructed lorebooks may be different editions; "
            "scores measure similarity and coverage, not exact hidden-book truth."
        ),
        "reference": {
            "sourceLorebookId": reference.get("sourceLorebookId"),
            "title": reference.get("title"),
            "contentFingerprint": hashlib.sha256(
                _normalize(reference_text).encode("utf-8")
            ).hexdigest(),
        },
        "reconstructed": {
            "sourceLorebookId": reconstructed.get("sourceLorebookId"),
            "title": reconstructed.get("title"),
            "contentFingerprint": hashlib.sha256(
                _normalize(recovered_text).encode("utf-8")
            ).hexdigest(),
        },
        "latestRecoveryRun": (
            reconstructed.get("recoveryRuns")[-1]
            if isinstance(reconstructed.get("recoveryRuns"), list)
            and reconstructed.get("recoveryRuns")
            else None
        ),
        "metrics": metrics,
        "alignmentThreshold": alignment_threshold,
        "referenceAttributionThreshold": reference_attribution_threshold,
        "matches": matches,
        "unmatchedReference": [
            {
                "uid": uid,
                "preview": " ".join(str(entry["content"]).split())[:160],
                "bestScore": max(
                    (
                        pair_metrics[index][column]["score"]
                        for column in range(len(recovered_items))
                    ),
                    default=0.0,
                ),
            }
            for index, (uid, entry) in enumerate(reference_items)
            if uid not in aligned_reference_uids
        ],
        "unmatchedRecovered": [
            {
                "uid": uid,
                "preview": " ".join(str(entry["content"]).split())[:160],
                "bestScore": max(
                    (
                        pair_metrics[row][index]["score"]
                        for row in range(len(reference_items))
                    ),
                    default=0.0,
                ),
            }
            for index, (uid, entry) in enumerate(recovered_items)
            if uid not in aligned_recovered_uids
        ],
    }


def load_benchmark_pair(
    lorebook_dir: Path,
    character_id: str,
    *,
    reference_id: str | None = None,
    reconstructed_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load or auto-select one public and one reconstructed lorebook record."""
    records: list[dict[str, Any]] = []
    for path in sorted(lorebook_dir.glob("*.json")):
        if path.name == "evidence.json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if character_id not in (record.get("characterIds") or []):
            continue
        record["_path"] = str(path)
        records.append(record)

    def select(explicit_id: str | None, field: str, label: str) -> dict[str, Any]:
        candidates = [
            record
            for record in records
            if _entry_map(record, field)
            and (
                explicit_id is None
                or str(record.get("sourceLorebookId")) == explicit_id
            )
        ]
        if len(candidates) != 1:
            ids = [str(record.get("sourceLorebookId")) for record in candidates]
            raise ValueError(
                f"expected one {label} lorebook for {character_id}, found "
                f"{len(candidates)} ({', '.join(ids) or 'none'}); pass its explicit ID"
            )
        return candidates[0]

    return (
        select(reference_id, "worldInfo", "reference"),
        select(reconstructed_id, "recoveredWorldInfo", "reconstructed"),
    )


def metric_deltas(
    report: dict[str, Any], baseline: dict[str, Any]
) -> dict[str, float]:
    """Return numeric current-minus-baseline metric deltas."""
    current_metrics = report.get("metrics") or {}
    baseline_metrics = baseline.get("metrics") or {}
    return {
        key: float(value) - float(baseline_metrics[key])
        for key, value in current_metrics.items()
        if isinstance(value, (int, float))
        and isinstance(baseline_metrics.get(key), (int, float))
    }


def benchmark_input_changes(
    report: dict[str, Any], baseline: dict[str, Any]
) -> dict[str, bool | None]:
    """Compare benchmark input fingerprints when both reports provide them."""
    changes: dict[str, bool | None] = {}
    for field in ("reference", "reconstructed"):
        current = str((report.get(field) or {}).get("contentFingerprint") or "")
        previous = str((baseline.get(field) or {}).get("contentFingerprint") or "")
        changes[f"{field}Changed"] = current != previous if current and previous else None
    return changes
