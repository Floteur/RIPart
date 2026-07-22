"""JanitorAI payload normalisation tests."""

from __future__ import annotations

from ripart.common.text import (
    normalize_sillytavern_identity_macros,
    normalize_user_placeholder,
)
from ripart.providers.janitor.payloads import (
    build_character,
    build_trigger_search_messages,
    separate,
)
from ripart.providers.janitor.payloads import _split_entries


def test_split_entries_keeps_headings_with_their_block():
    text = (
        "PASTE ONE OF THE LINKS BELOW\n\n"
        "* ![11](https://x/a.webp)\n* ![12](https://x/b.webp)\n\n"
        "> Picture Description\n\n"
        "11= park bench, yellow cardigan, sexy."
    )

    entries = _split_entries(text)

    # No heading stranded as its own bogus entry ...
    assert "> Picture Description" not in entries
    assert "PASTE ONE OF THE LINKS BELOW" not in entries
    # ... each heading rides with the block it introduces.
    assert any("PASTE ONE OF THE LINKS BELOW" in e and "a.webp" in e for e in entries)
    assert any("> Picture Description" in e and "11= park" in e for e in entries)


def test_normalize_user_placeholder_collapses_any_brace_count():
    assert (
        normalize_user_placeholder("{user} {{user}} {{{user}}} {{{{{user}}}}}")
        == "{{user}} {{user}} {{user}} {{user}}"
    )


def test_normalize_user_placeholder_preserves_other_sillytavern_macros():
    text = "{{char}} {{{user}}} {{random::red::blue}} {{getvar::{{char}}_mood}}"

    assert normalize_user_placeholder(text) == (
        "{{char}} {{user}} {{random::red::blue}} {{getvar::{{char}}_mood}}"
    )


def test_identity_macro_normaliser_repairs_char_and_legacy_aliases():
    assert (
        normalize_sillytavern_identity_macros(
            "{char} {{{bot}}} <USER> <BOT> <CHAR> {MYOS}"
        )
        == "{{char}} {{char}} {{user}} {{char}} {{char}} {MYOS}"
    )


def test_janitor_card_and_lorebook_text_use_sillytavern_user_macro():
    meta = {
        "showdefinition": True,
        "personality": "Talk to {{{user}}}.",
        "scenario": "Meet {user}.",
        "example_dialogs": "{{{{user}}}}: hello",
        "first_message": "Hi, {{{{{user}}}}}!",
        "description": "Notes for {user}.",
    }

    character = build_character(meta, {})
    lorebook = separate({"messages": [{"role": "system", "content": "{user} lore"}]})

    assert character["description"] == "Talk to {{user}}."
    assert character["scenario"] == "Meet {{user}}."
    assert character["exampleMessages"] == "{{user}}: hello"
    assert character["firstMessage"] == "Hi, {{user}}!"
    assert character["creatorNotes"] == "Notes for {{user}}."
    assert lorebook["entries"] == ["{{user}} lore"]


def test_gated_card_recovers_every_greeting_from_the_echo():
    # Gated card: JanitorAI nulls first_messages content, so the greetings only
    # survive as assistant turns in the echoed prompt. All must be captured, not
    # just the primary.
    meta = {"first_messages": [None, None], "name": "X"}
    payload = {
        "messages": [
            {"role": "system", "content": "<X's Persona>desc</X's Persona>"},
            {"role": "assistant", "content": "Greeting ONE"},
            {"role": "assistant", "content": "Greeting TWO"},
        ]
    }

    character = build_character(meta, payload, "", "desc")

    assert character["firstMessage"] == "Greeting ONE"
    assert character["alternateGreetings"] == ["Greeting TWO"]


def test_trigger_search_messages_prioritize_headings_and_distinctive_words():
    probes = build_trigger_search_messages(
        ["NEIGHBORHOOD CORE RULES\nEvery neighborhood has a distinctive bakery."]
    )

    candidates = [candidate for candidate, _message in probes]
    assert candidates[0] == "NEIGHBORHOOD CORE RULES"
    assert "NEIGHBORHOOD" in candidates
    assert all(candidate == message for candidate, message in probes)


def test_trigger_search_messages_are_distributed_across_entries():
    probes = build_trigger_search_messages(
        ["FIRST RULE\nMany first words.", "SECOND RULE\nMany second words."]
    )

    assert [candidate for candidate, _message in probes[:2]] == [
        "FIRST RULE",
        "SECOND RULE",
    ]
