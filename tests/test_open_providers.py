"""Contract tests for providers that do not require a live authenticated session."""

from __future__ import annotations

from ripart.providers import chub, spicychat, tavern


def test_chub_url_variants_normalize_to_a_full_path():
    assert chub.is_chub_url("https://characterhub.org/characters/alice/hero")
    assert chub.parse_full_path("https://chub.ai/characters/alice/hero") == "alice/hero"
    assert chub.parse_full_path("alice/hero") == "alice/hero"
    assert chub.parse_full_path("https://chub.ai/characters/alice") is None


def test_tavern_adapters_and_card_ids_are_deterministic():
    page = "https://character-tavern.com/character/creator/example-card"
    assert tavern.is_card_url(page)
    assert tavern.resolve_card_url(page) == "https://cards.character-tavern.com/creator/example-card.png"
    assert tavern.card_id_from_url("https://host.invalid/cards/A card.charx") == "A_card"
    assert tavern.is_card_url("https://host.invalid/cards/A card.txt") is False


def test_spicychat_url_parser_accepts_supported_routes_only():
    character_id = "a1b2c3d4-e5f6-7890-abcd-ef0123456789"
    assert spicychat.parse_character_id(f"https://spicychat.ai/chatbot/{character_id}") == character_id
    assert spicychat.parse_character_id(f"https://spicychat.ai/characters/{character_id}") == character_id
    assert spicychat.is_spicychat_url(f"https://spicychat.ai/chatbot/{character_id}")
    assert spicychat.parse_character_id("https://spicychat.ai/explore") is None
