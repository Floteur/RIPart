"""JanitorAI payload normalisation tests."""

from __future__ import annotations

from ripart.common.text import (
    normalize_sillytavern_identity_macros,
    normalize_user_placeholder,
)
from ripart.providers.janitor.payloads import (
    build_character,
    build_lorebook_trigger_messages,
    build_trigger_search_messages,
    separate,
)
from ripart.providers.janitor.payloads import _split_entries, parse_world_info_json


def test_parse_world_info_json_splits_entries_and_keeps_keys():
    # Echo shape from proxy mode: a JSON array of world-info objects, whose
    # content values carry unescaped quotes (6'5") that break strict json.loads.
    system = (
        "<X's Persona>[\n"
        '  {\n "content": "<Name> \\"Arkha\\" </name>\\n\\n'
        '<Appearance> \\"Tall, 195cm (6\'5"), dark skin.\\" </appearance>",\n'
        '   "inclusionGroup": ["Cleaner"],\n'
        '   "key": ["Arkha", "Corvus", "boss of the Cleaners"]\n  },\n'
        '  {\n "content": "<Name> \\"Enjin\\" </name>",\n'
        '   "key": ["Enjin"]\n  }\n]</Persona>'
    )
    entries = parse_world_info_json(system)

    assert len(entries) == 2  # two objects, not shattered on the blank line
    assert entries[0]["key"] == ["Arkha", "Corvus", "boss of the Cleaners"]
    assert entries[0]["inclusionGroup"] == ["Cleaner"]
    # unescaped quote inside content is preserved, escapes decoded
    assert '6\'5")' in entries[0]["content"]
    assert "\\n" not in entries[0]["content"]
    assert entries[1]["content"] == '<Name> "Enjin" </name>'
    # absent shape -> empty, so the caller falls back to blank-line splitting
    assert parse_world_info_json("no array here") == []


def test_merge_preserves_and_dedupes_json_entries():
    from ripart.providers.janitor.payloads import merge_separated_results

    a = {
        "entries": ["<Name> \"Arkha\" </name>"],
        "lorebookText": "",
        "jsonEntries": [{"content": '<Name> "Arkha" </name>', "key": ["Arkha"]}],
    }
    b = {  # same entry (dupe) + a new one, across a second pass
        "entries": ['<Name> "Arkha" </name>', '<Name> "Enjin" </name>'],
        "lorebookText": "",
        "jsonEntries": [
            {"content": '<Name> "Arkha" </name>', "key": ["Arkha"]},
            {"content": '<Name> "Enjin" </name>', "key": ["Enjin"]},
        ],
    }
    merged = merge_separated_results([a, b])
    keys = [k for e in merged["jsonEntries"] for k in e["key"]]
    assert keys == ["Arkha", "Enjin"]  # deduped by content, keys survive the merge


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


def test_broad_trigger_messages_never_emit_an_oversized_primary():
    messages = build_lorebook_trigger_messages(
        {"first_message": "greeting " * 50},
        "card " * 100,
        chunk_size=120,
    )

    assert messages
    assert max(map(len, messages)) <= 120


def test_trigger_search_messages_are_distributed_across_entries():
    probes = build_trigger_search_messages(
        ["FIRST RULE\nMany first words.", "SECOND RULE\nMany second words."]
    )

    assert [candidate for candidate, _message in probes[:2]] == [
        "FIRST RULE",
        "SECOND RULE",
    ]


def test_trigger_search_messages_reach_later_distinctive_words():
    probes = build_trigger_search_messages(
        [
            "alpha bravo charlie delta echo foxtrot golf hiddenkey "
            "ninthword"
        ]
    )

    candidates = [candidate.lower() for candidate, _message in probes]
    assert "hiddenkey" in candidates
    assert "ninthword" not in candidates
