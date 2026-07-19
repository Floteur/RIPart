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
