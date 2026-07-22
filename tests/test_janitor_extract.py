"""Janitor extraction guards that do not require a live browser."""

from __future__ import annotations

from ripart.providers.janitor import browser_tasks


def test_generate_summaries_report_shape_without_payload_content():
    body = {
        "chatMessages": [
            {"message": "private greeting", "is_bot": True},
            {"message": "secret trigger", "is_bot": False},
        ],
        "personas": [{"appearance": "private persona"}],
        "userConfig": {
            "api": "openai",
            "open_ai_mode": "proxy",
            "generation_settings": {"max_new_token": 2500},
        },
    }
    payload = {
        "max_tokens": 2500,
        "messages": [{"content": "private card and lorebook text"}],
    }

    request = browser_tasks._generate_request_summary(body)
    response = browser_tasks._generate_response_summary(payload)

    assert request == (
        "messages=2 chars=30 last=14 api=openai/proxy max_tokens=2500 persona=yes"
    )
    assert response == "messages=1 content_chars=30 parts=[30] max_tokens=2500"
    assert "secret trigger" not in request
    assert "private card" not in response
    error = str(browser_tasks.GenerateAlphaError(500, "private upstream response"))
    assert error == "generateAlpha failed: HTTP 500 response_bytes=25"
    assert "private upstream response" not in error


def test_trigger_search_matches_exclude_always_active_baseline_entries():
    recovered = ["Always active lore", "Triggered lore", "Other triggered lore"]
    constants = {browser_tasks._norm("Always active lore")}

    matches = browser_tasks._trigger_search_matches(
        ["Always active lore", "Triggered lore"], recovered, constants
    )

    assert matches == {browser_tasks._norm("Triggered lore")}


def test_trigger_search_debug_summary_is_semantic_and_bounded():
    long_entry = "Triggered lore " + "x" * 500
    recovered = ["Always active lore", long_entry, "Other triggered lore"]
    constants = {browser_tasks._norm("Always active lore")}

    summary = browser_tasks._trigger_search_debug_summary(
        "Parents", ["Always active lore", long_entry], recovered, constants
    )

    assert 'candidate="Parents"' in summary
    assert "found=2 baseline=1/1 matched=1" in summary
    assert "missing_baseline=0 unexpected=0" in summary
    assert "Triggered lore" in summary
    assert "x" * 100 not in summary
    assert len(summary) < 300


def test_trigger_activation_groups_capture_shared_probe_behavior():
    groups = browser_tasks._trigger_activation_groups(
        {
            "city entry": ["Minneapolis", "North Loop"],
            "dates entry": ["North Loop", "Minneapolis"],
            "family entry": ["parents"],
            "unmatched entry": [],
        }
    )

    assert groups == [
        {
            "triggers": ["Minneapolis", "North Loop"],
            "entries": ["city entry", "dates entry"],
        },
        {"triggers": ["parents"], "entries": ["family entry"]},
    ]
    summaries = browser_tasks._trigger_activation_groups_summary(
        {
            "city entry": ["Minneapolis", "North Loop"],
            "dates entry": ["North Loop", "Minneapolis"],
        }
    )
    assert len(summaries) == 1
    assert "activation group 1: entries=2" in summaries[0]
    assert 'triggers=["Minneapolis", "North Loop"]' in summaries[0]


def test_trigger_search_plateau_requires_full_coverage_and_positive_limit():
    searchable = ["Family lore", "City lore"]
    triggers = {
        browser_tasks._norm("Family lore"): ["parents"],
        browser_tasks._norm("City lore"): ["Minneapolis"],
    }

    assert browser_tasks._trigger_search_plateau_reached(searchable, triggers, 8, 8)
    assert not browser_tasks._trigger_search_plateau_reached(
        searchable, triggers, 7, 8
    )
    assert not browser_tasks._trigger_search_plateau_reached(
        searchable, triggers, 100, 0
    )
    del triggers[browser_tasks._norm("City lore")]
    assert not browser_tasks._trigger_search_plateau_reached(
        searchable, triggers, 100, 8
    )


def test_extract_does_not_turn_a_prompt_residue_into_lore_without_a_script(
    monkeypatch, capsys
):
    """Only an attached Janitor script authorizes closed-lore recovery."""
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "<Example Persona>Character definition.</Example Persona>\n\n"
                    "Prompt text outside the card wrappers."
                ),
            }
        ]
    }
    meta = {
        "id": "497eb143-1331-414a-8a72-9a3ada8d8df1",
        "name": "No Book",
        "allow_proxy": True,
        "scripts": [],
    }

    monkeypatch.setattr(browser_tasks, "_fetch_public_lorebooks", lambda *_: [])
    monkeypatch.setattr(browser_tasks, "_start_avatar_download", lambda *_: False)
    monkeypatch.setattr(browser_tasks, "_await_avatar_download", lambda *_: "")
    monkeypatch.setattr(browser_tasks, "_create_chat", lambda *_: "1")
    monkeypatch.setattr(browser_tasks, "_recover_chat_greetings", lambda *_: None)
    monkeypatch.setattr(browser_tasks, "_call_generate_alpha", lambda *_: payload)

    result = browser_tasks._extract_character(
        object(),
        meta["id"],
        f"https://janitorai.com/characters/{meta['id']}",
        profile={},
        persona=None,
        chunk_size=2500,
        max_trigger_passes=8,
        find_triggers=True,
        max_trigger_search_passes=48,
        trigger_search_miss_limit=8,
        settle=0,
        delete_chat_on_error=False,
        verbose=3,
        meta=meta,
    )

    assert result["entries"] == []
    assert result["lorebookText"] == ""
    assert result["publicLorebooks"] == []
    assert result["character"]["description"] == "Character definition."
    generation = result["diagnostics"]["generation"]
    assert generation["attempts"] == 1
    assert generation["succeeded"] == 1
    assert generation["rateLimits"] == 0
    assert generation["elapsedMs"] >= 0
    log = capsys.readouterr().out
    assert "probe generateAlpha 200" in log
    assert "request[messages=1" in log
    assert "response[messages=1 content_chars=" in log
    assert "Character definition." not in log
    assert "Prompt text outside the card wrappers." not in log
