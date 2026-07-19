"""Character Card and lorebook semantic round-trip coverage."""

from __future__ import annotations

from ripart.common.cards import build_card_v3, build_character_book, build_world_info
from ripart.common.tavern import card_to_result, lorebook_to_public


def test_world_info_builder_preserves_authored_sillytavern_options():
    world = build_world_info(
        [
            {
                "content": "Lore.",
                "key": ["lore"],
                "position": 4,
                "probability": 37,
                "sticky": 3,
                "caseSensitive": True,
                "characterFilter": {"isExclude": True, "names": ["Other"], "tags": []},
            }
        ]
    )

    entry = world["entries"]["0"]
    assert entry["position"] == 4
    assert entry["probability"] == 37
    assert entry["sticky"] == 3
    assert entry["caseSensitive"] is True
    assert entry["characterFilter"]["isExclude"] is True


def test_character_book_preserves_behavior_and_does_not_content_dedupe():
    public = {
        "title": "Setting",
        "description": "Book notes",
        "scanDepth": 8,
        "tokenBudget": 900,
        "recursiveScanning": True,
        "worldInfo": {
            "entries": {
                "0": {
                    "uid": 20,
                    "content": "Shared content.",
                    "key": ["first"],
                    "order": 250,
                    "position": 4,
                    "probability": 37,
                    "sticky": 3,
                    "caseSensitive": True,
                },
                "1": {
                    "uid": 21,
                    "content": "Shared content.",
                    "key": ["second"],
                    "order": 300,
                },
            }
        },
    }

    book = build_character_book(None, [public])

    assert book is not None
    assert book["name"] == "Setting"
    assert book["description"] == "Book notes"
    assert book["scan_depth"] == 8
    assert book["token_budget"] == 900
    assert book["recursive_scanning"] is True
    assert len(book["entries"]) == 2
    assert book["entries"][0]["insertion_order"] == 250
    assert book["entries"][0]["case_sensitive"] is True
    extensions = book["entries"][0]["extensions"]
    assert extensions["position"] == 4
    assert extensions["probability"] == 37
    assert extensions["sticky"] == 3
    assert extensions["case_sensitive"] is True
    assert extensions["ripart"]["sillytavern"]["position"] == 4
    assert book["entries"][0]["use_regex"] is True
    assert "probability" not in book["entries"][0]

    restored = lorebook_to_public(book)
    restored_entry = restored["worldInfo"]["entries"]["0"]
    assert restored_entry["position"] == 4
    assert restored_entry["probability"] == 37
    assert restored_entry["sticky"] == 3
    assert restored_entry["order"] == 250


def test_recovered_lore_is_disabled_when_activation_is_unknown():
    book = build_character_book(["Private recovered content."])

    assert book is not None
    entry = book["entries"][0]
    assert entry["enabled"] is False
    assert entry["constant"] is False
    assert entry["extensions"]["ripart"]["recovery"]["trigger_status"] == "unknown"


def test_recovered_lore_with_verified_trigger_is_enabled():
    book = build_character_book(
        ["Private recovered content."],
        recovered_triggers={"private recovered content.": ["Private"]},
    )

    assert book is not None
    entry = book["entries"][0]
    assert entry["keys"] == ["Private"]
    assert entry["enabled"] is True
    assert entry["extensions"]["ripart"]["recovery"]["trigger_status"] == "inferred"


def test_v3_card_round_trip_preserves_prompt_and_v3_fields():
    source = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "Aria",
            "description": "Description",
            "personality": "Personality",
            "scenario": "Scenario",
            "first_mes": "Hello",
            "mes_example": "<START>\n{{char}}: Hi",
            "creator_notes": "Notes",
            "system_prompt": "System override",
            "post_history_instructions": "Stay concise",
            "tags": ["Fantasy"],
            "creator": "Creator",
            "character_version": "2.4",
            "alternate_greetings": ["Alt"],
            "group_only_greetings": ["Group hello"],
            "nickname": "Ari",
            "creator_notes_multilingual": {"fr": "Remarques"},
            "source": ["urn:source:original"],
            "assets": [
                {"type": "icon", "uri": "https://example.test/icon.webp", "name": "main", "ext": "webp"}
            ],
            "creation_date": 123,
            "modification_date": 456,
            "extensions": {"example": {"kept": True}},
            "character_book": {
                "name": "World",
                "description": "Lore notes",
                "scan_depth": 7,
                "token_budget": 800,
                "recursive_scanning": True,
                "extensions": {"book": "kept"},
                "entries": [
                    {
                        "id": "entry-a",
                        "keys": ["moon"],
                        "secondary_keys": ["night"],
                        "comment": "Moon lore",
                        "content": "The moon is blue.",
                        "constant": False,
                        "selective": True,
                        "insertion_order": 42,
                        "enabled": True,
                        "position": "after_char",
                        "case_sensitive": True,
                        "name": "Moon",
                        "priority": 9,
                        "use_regex": True,
                        "extensions": {"entry": "kept"},
                    }
                ],
            },
        },
    }

    result = card_to_result(
        source,
        source_url="https://example.test/import",
        character_id="aria",
        definition_source="test",
    )
    exported = build_card_v3(
        result["character"],
        result["entries"],
        meta=result["meta"],
        public_lorebooks=result["publicLorebooks"],
        source_url=result["url"],
        timestamp=999,
    )["data"]

    assert exported["system_prompt"] == "System override"
    assert exported["post_history_instructions"] == "Stay concise"
    assert exported["creator_notes"] == "Notes"
    assert exported["group_only_greetings"] == ["Group hello"]
    assert exported["nickname"] == "Ari"
    assert exported["creator_notes_multilingual"] == {"fr": "Remarques"}
    assert exported["source"] == ["urn:source:original", "https://example.test/import"]
    assert exported["assets"] == source["data"]["assets"]
    assert exported["creation_date"] == 123
    assert exported["modification_date"] == 999
    assert exported["extensions"]["example"] == {"kept": True}
    book = exported["character_book"]
    assert book["scan_depth"] == 7
    assert book["token_budget"] == 800
    assert book["recursive_scanning"] is True
    assert book["entries"][0]["insertion_order"] == 42
    assert book["entries"][0]["position"] == "after_char"
    assert book["entries"][0]["use_regex"] is True
    assert book["entries"][0]["priority"] == 9
