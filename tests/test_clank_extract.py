"""Focused clank extraction result-shape tests."""

from __future__ import annotations

import importlib


def test_recovered_lore_uses_the_canonical_entries_field(monkeypatch):
    extract = importlib.import_module("ripart.providers.clank.extract")
    body = {"messages": []}
    monkeypatch.setattr(extract, "has_session", lambda: True)
    monkeypatch.setattr(extract, "parse_target", lambda _url: ("chat", "chat-1"))
    monkeypatch.setattr(extract, "get_chat_info", lambda _chat_id: {})
    monkeypatch.setattr(extract, "get_chat_messages", lambda _chat_id: [])
    monkeypatch.setattr(extract, "find_echo_body", lambda _messages: body)
    monkeypatch.setattr(extract, "_system_message", lambda _body: "definition")
    monkeypatch.setattr(extract, "split_definition", lambda _text: {"definition": "Character"})
    monkeypatch.setattr(extract, "_greeting_message", lambda _body: "Hello")
    monkeypatch.setattr(
        extract,
        "_build_result",
        lambda *_args, **_kwargs: {
            "character": {"creatorNotes": ""},
            "entries": [],
            "diagnostics": {},
        },
    )
    monkeypatch.setattr(extract, "dump_lorebook", lambda *_args, **_kwargs: ["Recovered."])

    result = extract.extract_chat(
        "https://clank.world/chat/chat-1",
        with_lorebook=True,
        lorebook_sleep=0,
    )

    assert result["entries"] == ["Recovered."]
    assert result["lorebookEntries"] == ["Recovered."]
    assert result["diagnostics"]["lorebookEntries"] == 1
