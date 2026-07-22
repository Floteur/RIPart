"""CLI contract tests for the JanitorAI provider group."""

from __future__ import annotations

import json

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
        }

    monkeypatch.setattr(cli, "OUT", tmp_path)
    monkeypatch.setattr(cli, "lorebook_task", fake_lorebook_task)

    result = CliRunner().invoke(
        cli.main, ["janitor", "lorebook", "book-42", "--limit", "1"]
    )

    assert result.exit_code == 0, result.output
    assert captured["lorebook_id"] == "book-42"
    assert "attached characters: 2" in result.output
    index = tmp_path / "lorebooks" / "book-42.json"
    assert index.exists()
    record = tmp_path / "library" / "lorebooks" / "janitor" / "book-42.json"
    assert record.exists()


def test_janitor_lorebook_benchmark_writes_offline_report(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "OUT", tmp_path)
    lorebook_dir = tmp_path / "library" / "lorebooks" / "janitor"
    lorebook_dir.mkdir(parents=True)
    public = {
        "sourceLorebookId": "public",
        "title": "Public",
        "characterIds": ["char-a"],
        "worldInfo": {
            "entries": {"0": {"content": "Maisie lives in Minneapolis."}}
        },
    }
    closed = {
        "sourceLorebookId": "closed",
        "title": "Closed",
        "characterIds": ["char-a"],
        "recoveredWorldInfo": {
            "entries": {"0": {"content": "Maisie lives in Minneapolis."}}
        },
    }
    (lorebook_dir / "public.json").write_text(json.dumps(public))
    (lorebook_dir / "closed.json").write_text(json.dumps(closed))

    result = CliRunner().invoke(
        cli.main, ["janitor", "benchmark-lorebook", "char-a"]
    )

    assert result.exit_code == 0, result.output
    assert "semantic token recall: 100.0%" in result.output
    report = tmp_path / "benchmarks" / "lorebooks" / "char-a.json"
    assert report.exists()


def test_janitor_lorebook_benchmark_merges_all_attached_blind_captures(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(cli, "OUT", tmp_path)
    reference = {
        "id": "shared-book",
        "title": "Shared",
        "worldInfo": {
            "entries": {
                "0": {"content": "True constant", "constant": True},
                "1": {"content": "Alpha lore", "key": ["Alpha"]},
                "2": {"content": "Beta lore", "key": ["Beta"]},
            }
        },
    }

    def fake_lorebook_task(data, **_browser_options):
        assert data == {"lorebook_id": "shared-book"}
        return {
            "lorebook": reference,
            "characters": [{"id": "char-a"}, {"id": "char-b"}],
        }

    def fake_extract_task(data, **_browser_options):
        character = data["url"]
        suffix = "Alpha" if character == "char-a" else "Beta"
        return {
            "characterId": character,
            "entries": ["True constant", f"{suffix} lore"],
            "recoveredConstants": ["True constant", f"{suffix} lore"],
            "recoveredTriggers": {f"{suffix.lower()} lore": [suffix]},
            "benchmarkReferenceLorebook": reference,
        }

    monkeypatch.setattr(cli, "lorebook_task", fake_lorebook_task)
    monkeypatch.setattr(cli, "extract_task", fake_extract_task)

    result = CliRunner().invoke(
        cli.main,
        [
            "janitor",
            "benchmark-lorebook",
            "https://janitorai.com/scripts/shared-book",
            "--capture",
            "--all-attached",
        ],
    )

    assert result.exit_code == 0, result.output
    report_path = tmp_path / "benchmarks" / "lorebooks" / "shared-book-blind.json"
    report = json.loads(report_path.read_text())
    assert report["benchmarkType"] == "lorebook-blind-ground-truth"
    assert report["captureCharacterIds"] == ["char-a", "char-b"]
    assert len(report["capturedWorldInfo"]["entries"]) == 3
    assert report["metrics"]["constantPrecision"] == 1.0


def test_janitor_lorebook_benchmark_warns_when_baseline_input_is_unchanged(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(cli, "OUT", tmp_path)
    lorebook_dir = tmp_path / "library" / "lorebooks" / "janitor"
    lorebook_dir.mkdir(parents=True)
    public = {
        "sourceLorebookId": "public",
        "title": "Public",
        "characterIds": ["char-a"],
        "worldInfo": {"entries": {"0": {"content": "Public known fact."}}},
    }
    closed = {
        "sourceLorebookId": "closed",
        "title": "Closed",
        "characterIds": ["char-a"],
        "recoveredWorldInfo": {
            "entries": {"0": {"content": "Public known fact."}}
        },
    }
    (lorebook_dir / "public.json").write_text(json.dumps(public))
    (lorebook_dir / "closed.json").write_text(json.dumps(closed))
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(cli.benchmark_lorebooks(public, closed)))

    result = CliRunner().invoke(
        cli.main,
        [
            "janitor",
            "benchmark-lorebook",
            "char-a",
            "--baseline",
            str(baseline),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "reconstructed content fingerprint is identical" in result.output


def test_provider_extract_adapter_binds_current_cli_output_directory(
    monkeypatch, tmp_path
):
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


def test_root_help_has_no_janitor_legacy_aliases():
    result = CliRunner().invoke(cli.main, ["--help"])

    assert result.exit_code == 0, result.output
    assert "JanitorAI (legacy aliases)" not in result.output
    assert "import-session" not in result.output
    assert "rip janitor" in result.output
