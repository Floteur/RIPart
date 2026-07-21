"""Janitor extraction guards that do not require a live browser."""

from __future__ import annotations

from ripart.providers.janitor import browser_tasks


def test_extract_does_not_turn_a_prompt_residue_into_lore_without_a_script(
    monkeypatch,
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
        settle=0,
        delete_chat_on_error=False,
        verbose=0,
        meta=meta,
    )

    assert result["entries"] == []
    assert result["lorebookText"] == ""
    assert result["publicLorebooks"] == []
    assert result["character"]["description"] == "Character definition."
