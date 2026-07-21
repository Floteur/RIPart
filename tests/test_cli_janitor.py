"""CLI contract tests for the JanitorAI provider group."""

from __future__ import annotations

from click.testing import CliRunner

import ripart.cli as cli


def test_janitor_list_extracts_with_automatic_fallback(monkeypatch, tmp_path):
    """Bulk extraction enables the proxy-disabled fallback unless opted out."""
    captured: dict = {}

    def fake_recent_task(data, **browser_options):
        captured.update(data)
        captured["browser_options"] = browser_options
        return {"cards": [], "extracted": []}

    monkeypatch.setattr(cli, "OUT", tmp_path)
    monkeypatch.setattr(cli, "recent_task", fake_recent_task)

    result = CliRunner().invoke(cli.main, ["janitor", "list", "--extract"])

    assert result.exit_code == 0, result.output
    assert captured["extract"] is True
    assert captured["jllm_leak"] is True
    assert captured["checkpoint_library_dir"] == str(tmp_path / "library")


def test_janitor_list_can_disable_the_fallback(monkeypatch, tmp_path):
    """Users can avoid lossy reconstructions when they only want exact captures."""
    captured: dict = {}

    def fake_recent_task(data, **_browser_options):
        captured.update(data)
        return {"cards": [], "extracted": []}

    monkeypatch.setattr(cli, "OUT", tmp_path)
    monkeypatch.setattr(cli, "recent_task", fake_recent_task)

    result = CliRunner().invoke(
        cli.main, ["janitor", "list", "--extract", "--no-jllm-leak"]
    )

    assert result.exit_code == 0, result.output
    assert captured["jllm_leak"] is False


def test_janitor_lorebook_indexes_all_provider_linked_characters(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_lorebook_task(data, **browser_options):
        captured.update(data)
        captured["browser_options"] = browser_options
        return {
            "url": "https://janitorai.com/hampter/script/book-42",
            "lorebook": {
                "id": "book-42",
                "title": "Shared setting",
                "accessible": False,
                "worldInfo": {"entries": {}},
                "referencedCharacters": [
                    {
                        "id": "char-a",
                        "name": "Alpha",
                        "url": "https://janitorai.com/characters/char-a",
                    },
                    {
                        "id": "char-b",
                        "name": "Beta",
                        "url": "https://janitorai.com/characters/char-b",
                    },
                ],
            },
            "characters": [
                {"id": "char-a", "name": "Alpha", "url": "https://janitorai.com/characters/char-a"},
                {"id": "char-b", "name": "Beta", "url": "https://janitorai.com/characters/char-b"},
            ],
        }

    monkeypatch.setattr(cli, "OUT", tmp_path)
    monkeypatch.setattr(cli, "lorebook_task", fake_lorebook_task)

    result = CliRunner().invoke(cli.main, ["janitor", "lorebook", "book-42", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert captured["lorebook_id"] == "book-42"
    assert "attached characters: 2" in result.output
    index = tmp_path / "lorebooks" / "book-42.json"
    assert index.exists()
    record = tmp_path / "library" / "lorebooks" / "janitor" / "book-42.json"
    assert record.exists()


def test_provider_extract_adapter_binds_current_cli_output_directory(monkeypatch, tmp_path):
    """Provider workflows receive UI services instead of importing the Click module."""
    captured: dict = {}

    def fake_extract(ui, url, **kwargs):
        captured["library_dir"] = ui.library_dir
        captured["url"] = url
        captured["kwargs"] = kwargs

    monkeypatch.setattr(cli, "OUT", tmp_path)
    monkeypatch.setattr(cli.cli_extractors, "saucepan_extract", fake_extract)

    cli._saucepan_extract("companion-42", leak=True, verbose=2)

    assert captured == {
        "library_dir": tmp_path / "library",
        "url": "companion-42",
        "kwargs": {"leak": True, "verbose": 2},
    }
