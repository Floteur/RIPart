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
    record = json.loads((tmp_path / "lorebooks" / "janitor" / "book-42.json").read_text())
    assert record["sourceLorebookId"] == "book-42"
    assert record["characterIds"] == ["char-a", "char-b"]
    assert record["worldInfo"]["entries"] == book["worldInfo"]["entries"]
    assert record["entryCount"] == 1


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


def test_private_entries_are_kept_as_unassigned_observations(tmp_path):
    result = _result({"title": "empty", "worldInfo": {"entries": {}}})
    result["entries"] = ["Private setting detail.", "Private setting detail."]

    update_lorebook_library(tmp_path, "char-private", result)

    observation = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "unassigned" / "char-private.json").read_text()
    )
    assert observation["characterId"] == "char-private"
    assert observation["observations"] == [
        {
            "content": "Private setting detail.",
            "contentFingerprint": observation["observations"][0]["contentFingerprint"],
            "attribution": {"status": "unassigned", "candidates": []},
        }
    ]


def test_private_observations_accumulate_across_extractions(tmp_path):
    result = _result({"title": "empty", "worldInfo": {"entries": {}}})
    result["entries"] = ["First recovered detail."]
    update_lorebook_library(tmp_path, "char-private", result)
    result["entries"] = ["Second recovered detail."]
    update_lorebook_library(tmp_path, "char-private", result)

    observation = json.loads(
        (tmp_path / "lorebooks" / "janitor" / "unassigned" / "char-private.json").read_text()
    )
    assert [item["content"] for item in observation["observations"]] == [
        "First recovered detail.",
        "Second recovered detail.",
    ]


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
