"""Tests for the offline reconstructed-lorebook benchmark."""

from __future__ import annotations

import json

from ripart.common.lorebook_benchmark import (
    benchmark_input_changes,
    benchmark_lorebooks,
    load_benchmark_pair,
    merge_blind_captures,
    metric_deltas,
    reconstructed_record_from_capture,
    injectable_reference_record,
)


def _record(book_id: str, field: str, contents: list[str]) -> dict:
    return {
        "sourceLorebookId": book_id,
        "title": book_id,
        "characterIds": ["char-a"],
        field: {
            "entries": {
                str(index): {"content": content}
                for index, content in enumerate(contents)
            }
        },
    }


def test_benchmark_aligns_entries_and_reports_coverage():
    reference = _record(
        "public",
        "worldInfo",
        [
            "Maisie lives in Minneapolis and explores independent cafes.",
            "Her parents Mary and James had a difficult relationship.",
        ],
    )
    reconstructed = _record(
        "closed",
        "recoveredWorldInfo",
        [
            "Mary and James are Maisie's parents with a difficult relationship.",
            "Maisie explores Minneapolis cafes.",
        ],
    )

    report = benchmark_lorebooks(reference, reconstructed)

    assert report["benchmarkType"] == "lorebook-silver-standard"
    assert report["metrics"]["referenceEntries"] == 2
    assert report["metrics"]["recoveredEntries"] == 2
    assert report["metrics"]["tokenRecall"] > 0.7
    assert report["metrics"]["referenceCoverageAt25"] == 1.0
    assert report["metrics"]["unmatchedReferenceEntries"] == 0
    assert report["reference"]["contentFingerprint"]
    assert report["reconstructed"]["contentFingerprint"]
    assert {(match["referenceUid"], match["recoveredUid"]) for match in report["matches"]} == {
        ("0", "1"),
        ("1", "0"),
    }


def test_load_pair_auto_selects_public_and_reconstructed_records(tmp_path):
    public = _record("public", "worldInfo", ["Known public lore."])
    closed = _record("closed", "recoveredWorldInfo", ["Recovered lore."])
    (tmp_path / "public.json").write_text(json.dumps(public))
    (tmp_path / "closed.json").write_text(json.dumps(closed))

    selected_public, selected_closed = load_benchmark_pair(tmp_path, "char-a")

    assert selected_public["sourceLorebookId"] == "public"
    assert selected_closed["sourceLorebookId"] == "closed"


def test_metric_deltas_compare_numeric_metrics_only():
    deltas = metric_deltas(
        {"metrics": {"tokenRecall": 0.8, "referenceEntries": 12}},
        {"metrics": {"tokenRecall": 0.6, "referenceEntries": 11}},
    )

    assert round(deltas["tokenRecall"], 6) == 0.2
    assert deltas["referenceEntries"] == 1.0


def test_benchmark_input_changes_detect_identical_reconstruction():
    current = {
        "reference": {"contentFingerprint": "same-reference"},
        "reconstructed": {"contentFingerprint": "same-reconstruction"},
    }
    baseline = {
        "reference": {"contentFingerprint": "same-reference"},
        "reconstructed": {"contentFingerprint": "same-reconstruction"},
    }

    assert benchmark_input_changes(current, baseline) == {
        "referenceChanged": False,
        "reconstructedChanged": False,
    }


def test_blind_capture_record_preserves_inferred_triggers_and_constants():
    result = {
        "entries": ["Always lore", "Triggered lore"],
        "recoveredConstants": ["Always lore"],
        "recoveredTriggers": {"triggered lore": ["secret"]},
    }

    record = reconstructed_record_from_capture(result)
    entries = record["recoveredWorldInfo"]["entries"]

    assert entries["0"]["constant"] is True
    assert entries["0"]["key"] == []
    assert entries["1"]["constant"] is False
    assert entries["1"]["key"] == ["secret"]


def test_blind_reference_excludes_entries_the_server_will_never_inject():
    reference = _record(
        "public", "worldInfo", ["Enabled lore", "Disabled lore"]
    )
    reference["worldInfo"]["entries"]["0"]["enabled"] = True
    reference["worldInfo"]["entries"]["1"]["enabled"] = False
    reference["worldInfo"]["entries"]["1"]["disable"] = True

    injectable, excluded = injectable_reference_record(reference)

    assert list(injectable["worldInfo"]["entries"]) == ["0"]
    assert excluded == 1
    assert injectable["sourceLorebookId"] == "public"


def test_blind_reference_copies_provider_id_into_report_identity():
    reference = _record("unused", "worldInfo", ["Known lore"])
    reference.pop("sourceLorebookId")
    reference["id"] = "provider-book"

    injectable, _excluded = injectable_reference_record(reference)

    assert injectable["sourceLorebookId"] == "provider-book"


def test_merge_blind_captures_unions_entries_and_intersects_baselines():
    merged = merge_blind_captures(
        [
            {
                "characterId": "a",
                "entries": ["True constant", "Card A lore"],
                "recoveredConstants": ["True constant", "Card A lore"],
                "recoveredTriggers": {"card a lore": ["Alpha"]},
            },
            {
                "characterId": "b",
                "entries": ["True constant", "Card B lore"],
                "recoveredConstants": ["True constant", "Card B lore"],
                "recoveredTriggers": {"card b lore": ["Beta"]},
            },
        ]
    )

    assert merged["entries"] == ["True constant", "Card A lore", "Card B lore"]
    assert merged["recoveredConstants"] == ["True constant"]
    assert merged["recoveredTriggers"] == {
        "card a lore": ["Alpha"],
        "card b lore": ["Beta"],
    }
    assert merged["captureCharacterIds"] == ["a", "b"]


def test_trigger_recall_includes_unmatched_reference_entries():
    reference = _record("public", "worldInfo", ["Known", "Missing section"])
    reference["worldInfo"]["entries"]["0"]["key"] = ["known-key"]
    reference["worldInfo"]["entries"]["1"]["key"] = ["missing-key"]
    recovered = _record("capture", "recoveredWorldInfo", ["Known"])
    recovered["recoveredWorldInfo"]["entries"]["0"]["key"] = ["known-key"]

    report = benchmark_lorebooks(reference, recovered)

    assert report["metrics"]["referenceTriggerKeys"] == 2
    assert report["metrics"]["triggerRecall"] == 0.5


def test_split_aware_coverage_recognizes_one_reference_split_into_fragments():
    reference = _record(
        "public",
        "worldInfo",
        ["alpha bravo charlie delta echo foxtrot golf hotel"],
    )
    recovered = _record(
        "capture",
        "recoveredWorldInfo",
        ["alpha bravo charlie delta", "echo foxtrot golf hotel"],
    )

    report = benchmark_lorebooks(reference, recovered)

    assert report["metrics"]["meanSplitAwareTokenCoverage"] == 1.0
    assert report["metrics"]["meanSplitAwareChar8Coverage"] == 1.0
    assert report["metrics"]["referenceAttributedRecoveredEntries"] == 2
