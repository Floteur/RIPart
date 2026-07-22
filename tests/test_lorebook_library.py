"""Tests for persistent, reusable library lorebook records."""

from __future__ import annotations

import json

from ripart.common.lorebooks import update_lorebook_library


def _result(book: dict) -> dict:
    return {
        "url": "https://janitorai.com/characters/example",
        "publicLorebooks": [book],
    }


def test_lorebook_record_is_shared_by_source_id_and_tracks_characters(tmp_path):
    book = {
        "id": "book-42",
        "title": "Shared setting",
        "description": "The canonical shared setting.",
        "worldInfo": {
            "entries": {
                "0": {
                    "content": "The city is built around a volcano.",
                    "key": ["city"],
                    "keysecondary": ["volcano"],
                    "comment": "Setting",
                }
            }
        },
    }
    first = update_lorebook_library(tmp_path, "char-a", _result(book))
    second = update_lorebook_library(tmp_path, "char-b", _result(book))

    assert first == second
    record = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "book-42.json").read_text()
    )
    assert record["sourceLorebookId"] == "book-42"
    assert record["description"] == "The canonical shared setting."
    assert record["characterIds"] == ["char-a", "char-b"]
    assert record["worldInfo"]["entries"] == book["worldInfo"]["entries"]
    assert record["entryCount"] == 1


def test_lorebook_refresh_preserves_description_when_api_omits_it(tmp_path):
    book = {
        "id": "book-42",
        "description": "Description from the provider API.",
        "worldInfo": {"entries": {}},
    }
    update_lorebook_library(tmp_path, "char-a", _result(book))
    book.pop("description")

    update_lorebook_library(tmp_path, "char-a", _result(book))

    record = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "book-42.json").read_text()
    )
    assert record["description"] == "Description from the provider API."


def test_lorebook_record_uses_content_fingerprint_when_no_provider_id(tmp_path):
    book = {
        "title": "Imported book",
        "worldInfo": {"entries": {"0": {"content": "A fact."}}},
    }
    paths = update_lorebook_library(tmp_path, "char-a", _result(book))

    assert len(paths) == 1
    record = json.loads(open(paths[0], encoding="utf-8").read())
    assert record["sourceLorebookId"] is None
    assert record["contentFingerprint"] in paths[0]


def test_private_entries_need_an_attached_lorebook_to_be_recorded(tmp_path):
    # No attached lorebook: recovered blocks can never be attributed to a book,
    # so no observation record is written (nothing to grab a lorebook for).
    result = _result({"title": "empty", "worldInfo": {"entries": {}}})
    result["entries"] = ["Private setting detail."]
    update_lorebook_library(tmp_path, "char-nobook", result)
    assert not (
        tmp_path / "lorebooks" / "janitor" / "unassigned" / "char-nobook.json"
    ).exists()

    # With an attached (private) lorebook id, the same blocks are kept for later
    # cross-character attribution.
    result = _result({"id": "book-9", "title": "Private", "worldInfo": {"entries": {}}})
    result["entries"] = ["Private setting detail.", "Private setting detail."]
    update_lorebook_library(tmp_path, "char-private", result)
    observation = json.loads(
        (
            tmp_path / "lorebooks" / "janitor" / "unassigned" / "char-private.json"
        ).read_text()
    )
    assert observation["characterId"] == "char-private"
    assert observation["observations"] == [
        {
            "content": "Private setting detail.",
            "contentFingerprint": observation["observations"][0]["contentFingerprint"],
            "attribution": {"status": "inferred", "candidates": ["book-9"]},
        }
    ]


def test_private_observations_accumulate_across_extractions(tmp_path):
    result = _result({"id": "book-1", "title": "Private", "worldInfo": {"entries": {}}})
    result["entries"] = ["First recovered detail."]
    update_lorebook_library(tmp_path, "char-private", result)
    result["entries"] = ["Second recovered detail."]
    update_lorebook_library(tmp_path, "char-private", result)

    observation = json.loads(
        (
            tmp_path / "lorebooks" / "janitor" / "unassigned" / "char-private.json"
        ).read_text()
    )
    assert [item["content"] for item in observation["observations"]] == [
        "First recovered detail.",
        "Second recovered detail.",
    ]


def test_private_observation_is_attributed_when_shared_characters_leave_one_book(
    tmp_path,
):
    first = _result({"id": "shared", "title": "Shared", "worldInfo": {"entries": {}}})
    first["publicLorebooks"].append(
        {"id": "other", "title": "Other", "worldInfo": {"entries": {}}}
    )
    first["entries"] = ["Shared private fact."]
    update_lorebook_library(tmp_path, "char-a", first)

    second = _result({"id": "shared", "title": "Shared", "worldInfo": {"entries": {}}})
    second["entries"] = ["Shared private fact."]
    update_lorebook_library(tmp_path, "char-b", second)

    record = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "shared.json").read_text()
    )
    assert record["recoveredObservations"][0]["content"] == "Shared private fact."
    evidence = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "evidence.json").read_text()
    )
    item = next(iter(evidence["observations"].values()))
    assert item["attribution"] == {"status": "inferred", "candidates": ["shared"]}
    first_capture = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "unassigned" / "char-a.json").read_text()
    )
    assert first_capture["observations"][0]["attribution"] == {
        "status": "inferred",
        "candidates": ["shared"],
    }


def test_recovered_entries_are_attributed_only_to_closed_attachment(tmp_path):
    result = {
        "url": "https://janitorai.com/characters/example",
        "publicLorebooks": [
            {
                "id": "public-book",
                "title": "Readable",
                "accessible": True,
                "worldInfo": {"entries": {"0": {"content": "Public fact."}}},
            },
            {
                "id": "closed-book",
                "title": "Closed",
                "accessible": False,
                "worldInfo": {"entries": {}},
            },
        ],
        "entries": ["Always private.", "Triggered private.", "Unknown private."],
        "recoveredConstants": ["Always private."],
        "recoveredTriggers": {
            "triggered private.": ["secret", "private fact"]
        },
        "diagnostics": {
            "triggerPasses": [
                {
                    "index": 1,
                    "chars": 100,
                    "entriesFound": 3,
                    "newEntries": 3,
                    "loreChars": 50,
                }
            ],
            "triggerSearchPasses": 7,
            "triggerSearchCandidates": 12,
            "triggerSearchMissLimit": 8,
            "triggerActivationGroups": 1,
            "generation": {
                "attempts": 10,
                "succeeded": 9,
                "rateLimits": 1,
                "elapsedMs": 321.5,
            },
        },
    }

    update_lorebook_library(tmp_path, "char-a", result)

    closed = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "closed-book.json").read_text()
    )
    assert closed["recoveredEntryCount"] == 3
    assert closed["recoveredObservations"][0]["attribution"] == {
        "status": "inferred",
        "candidates": ["closed-book"],
    }
    entries = closed["recoveredWorldInfo"]["entries"]
    assert entries["0"]["constant"] is True
    assert entries["0"]["key"] == []
    assert entries["1"]["constant"] is False
    assert entries["1"]["key"] == ["secret", "private fact"]
    assert entries["1"]["disable"] is False
    assert entries["2"]["constant"] is False
    assert entries["2"]["key"] == []
    assert entries["2"]["disable"] is True
    assert entries["2"]["extensions"]["ripart"]["activation"] == "unknown"
    public = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "public-book.json").read_text()
    )
    assert public["recoveredObservations"] == []
    run = closed["recoveryRuns"][-1]
    assert run["recoveredEntries"] == 3
    assert run["alwaysActiveEntries"] == 1
    assert run["entriesWithInferredKeys"] == 1
    assert run["uniqueInferredKeys"] == 2
    assert run["triggerPasses"][0]["newEntries"] == 3
    assert run["triggerSearchProbes"] == 7
    assert run["generateAttempts"] == 10
    assert run["rateLimits"] == 1


def test_new_visibility_replaces_stale_broad_attribution_sighting(tmp_path):
    result = {
        "url": "https://janitorai.com/characters/example",
        "publicLorebooks": [
            {
                "id": "book-a",
                "accessible": False,
                "worldInfo": {"entries": {}},
            },
            {
                "id": "book-b",
                "accessible": False,
                "worldInfo": {"entries": {}},
            },
        ],
        "entries": ["Recovered fact."],
    }
    update_lorebook_library(tmp_path, "char-a", result)

    result["publicLorebooks"][0].update(
        {
            "accessible": True,
            "isCodePublic": True,
            "worldInfo": {"entries": {"0": {"content": "Public fact."}}},
        }
    )
    result["publicLorebooks"][1]["isCodePublic"] = False
    update_lorebook_library(tmp_path, "char-a", result)

    record = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "book-b.json").read_text()
    )
    assert record["recoveredObservations"][0]["attribution"] == {
        "status": "inferred",
        "candidates": ["book-b"],
    }


def test_inaccessible_lorebook_reference_is_saved_for_later_reconciliation(tmp_path):
    result = _result(
        {
            "id": "private-book",
            "title": "Not readable here",
            "accessible": False,
            "worldInfo": {"entries": {}},
        }
    )

    paths = update_lorebook_library(tmp_path, "char-private", result)

    assert paths == [str(tmp_path / "lorebooks" / "janitor" / "private-book.json")]
    record = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "private-book.json").read_text()
    )
    assert record["entryCount"] == 0
    assert record["accessible"] is False
    assert record["characterIds"] == ["char-private"]


def test_lorebook_record_keeps_provider_character_index_for_later_regeneration(
    tmp_path,
):
    book = {
        "id": "shared-world",
        "title": "Shared world",
        "referencedCharacters": [
            {"id": "char-a", "name": "Alpha", "url": "https://example/a"},
            {"id": "char-b", "name": "Beta", "creator": "Creator"},
        ],
        "worldInfo": {"entries": {}},
    }

    update_lorebook_library(tmp_path, "char-a", _result(book))
    record = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "shared-world.json").read_text()
    )

    assert record["characterIds"] == ["char-a"]
    assert record["referencedCharacters"] == book["referencedCharacters"]


def test_lorebook_refresh_preserves_recovered_observations(tmp_path):
    book = {"id": "shared-world", "worldInfo": {"entries": {}}}
    result = _result(book)
    result["entries"] = ["Recovered private fact."]
    update_lorebook_library(tmp_path, "char-a", result)
    result["entries"] = []
    update_lorebook_library(tmp_path, "char-a", result)

    record = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "shared-world.json").read_text()
    )
    assert record["recoveredObservations"][0]["content"] == "Recovered private fact."


def test_fingerprint_distinguishes_behaviorally_different_books(tmp_path):
    first = {
        "title": "Imported book",
        "worldInfo": {
            "entries": {"0": {"content": "A fact.", "key": ["fact"], "probability": 25}}
        },
    }
    second = {
        "title": "Imported book",
        "worldInfo": {
            "entries": {"0": {"content": "A fact.", "key": ["fact"], "probability": 75}}
        },
    }

    first_path = update_lorebook_library(tmp_path, "char-a", _result(first))[0]
    second_path = update_lorebook_library(tmp_path, "char-b", _result(second))[0]

    assert first_path != second_path


def test_duplicate_source_uids_do_not_overwrite_entries(tmp_path):
    book = {
        "id": "duplicates",
        "worldInfo": {
            "entries": [
                {"uid": 7, "content": "First."},
                {"uid": 7, "content": "Second."},
            ]
        },
    }
    path = update_lorebook_library(tmp_path, "char-a", _result(book))[0]
    record = json.loads(open(path, encoding="utf-8").read())

    assert list(record["worldInfo"]["entries"]) == ["7", "7-2"]
