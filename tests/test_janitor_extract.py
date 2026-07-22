"""Janitor extraction guards that do not require a live browser."""

from __future__ import annotations

import io

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


def test_write_generate_dump_captures_query_answer_and_http_stats():
    fh = io.StringIO()
    body = {"chatMessages": [{"role": "user", "message": "trigger text"}]}
    payload = {"messages": [{"content": "recovered lore"}]}
    browser_tasks._write_generate_dump(
        fh, "char-1", "probe", 200, 123.0, body, "raw sse body", payload
    )
    out = fh.getvalue()
    # everything the --dump flag promises: query, raw + parsed answer, HTTP stats
    assert "trigger text" in out
    assert "raw sse body" in out
    assert "recovered lore" in out
    assert "HTTP 200" in out
    assert "response_bytes=12" in out  # len("raw sse body")


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


def test_blind_benchmark_rejects_public_books_whose_entries_are_disabled():
    book = {
        "id": "disabled-book",
        "isCodePublic": True,
        "worldInfo": {
            "entries": {
                "0": {"content": "Known lore", "enabled": False, "disable": True}
            }
        },
    }

    try:
        browser_tasks._select_blind_benchmark_lorebooks([book], "disabled-book")
    except RuntimeError as exc:
        assert "all 1 public entries are disabled" in str(exc)
    else:
        raise AssertionError("disabled public book should be ineligible")


def test_blind_benchmark_selects_one_enabled_public_book():
    book = {
        "id": "enabled-book",
        "isCodePublic": True,
        "worldInfo": {
            "entries": {
                "0": {"content": "Known lore", "enabled": True, "disable": False}
            }
        },
    }

    assert browser_tasks._select_blind_benchmark_lorebooks([book], "enabled-book") == [
        book
    ]


def test_blind_benchmark_auto_selects_all_public_books():
    books = [
        {
            "id": "book-a",
            "isCodePublic": True,
            "title": "A",
            "worldInfo": {"entries": {"0": {"content": "Alpha lore", "enabled": True}}},
        },
        {
            "id": "book-b",
            "isCodePublic": True,
            "title": "B",
            "worldInfo": {"entries": {"0": {"content": "Beta lore", "enabled": True}}},
        },
        {"id": "closed", "isCodePublic": False, "worldInfo": {"entries": {}}},
    ]

    selected = browser_tasks._select_blind_benchmark_lorebooks(books, "")
    assert [book["id"] for book in selected] == ["book-a", "book-b"]

    merged = browser_tasks._merge_reference_books(selected)
    assert merged["id"] == "book-a+book-b"
    assert len(merged["worldInfo"]["entries"]) == 2


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
        blind_lorebook_benchmark_id=None,
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


def test_extract_skips_leak_when_attached_lorebook_is_fully_public(
    monkeypatch, capsys
):
    """Code-public books arrive whole from /script - never re-leak them.

    Definition is absent from meta (so the probe still runs for the card), but
    every attached book is code-public, so no trigger passes or trigger-search
    generateAlpha calls should fire and no lore should be "recovered".
    """
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "<Example Persona>Character definition.</Example Persona>\n\n"
                    "# LOW TIDE\n\nLow Tide is a five-piece alternative rock band."
                ),
            }
        ]
    }
    book = {
        "id": "book1",
        "title": "Low Tide",
        "accessible": True,
        "isPublic": True,
        "isCodePublic": True,
        "entryCount": 1,
        "worldInfo": {
            "entries": {
                "0": {
                    "uid": 0,
                    "content": "# LOW TIDE\n\nLow Tide is a five-piece alternative rock band.",
                    "key": ["Low Tide", "band"],
                    "comment": "Low Tide",
                }
            }
        },
    }
    meta = {
        "id": "49b7e07d-7898-4715-bbbe-13d736dd89a3",
        "name": "Abby",
        "allow_proxy": True,
        "scripts": [{"id": "book1", "type": "lorebook", "title": "Low Tide"}],
    }

    calls = {"n": 0}

    def _one_generate(*_):
        calls["n"] += 1
        return payload

    monkeypatch.setattr(browser_tasks, "_fetch_public_lorebooks", lambda *_: [book])
    monkeypatch.setattr(browser_tasks, "_start_avatar_download", lambda *_: False)
    monkeypatch.setattr(browser_tasks, "_await_avatar_download", lambda *_: "")
    monkeypatch.setattr(browser_tasks, "_create_chat", lambda *_: "1")
    monkeypatch.setattr(browser_tasks, "_recover_chat_greetings", lambda *_: None)
    monkeypatch.setattr(browser_tasks, "_delete_chat", lambda *_: True)
    monkeypatch.setattr(browser_tasks, "_call_generate_alpha", _one_generate)

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
        blind_lorebook_benchmark_id=None,
        settle=0,
        delete_chat_on_error=False,
        verbose=3,
        meta=meta,
    )

    # Only the card probe fired - no trigger passes, no trigger-search probes.
    assert calls["n"] == 1
    assert result["entries"] == []
    assert result["lorebookText"] == ""
    assert result["publicLorebooks"] == [book]
    assert result["diagnostics"]["triggerPasses"] == []
    assert result["diagnostics"]["triggerSearchPasses"] == 0
    log = capsys.readouterr().out
    assert "lorebook fully public" in log
    assert "lorebook trigger pass" not in log
    assert "trigger research" not in log
