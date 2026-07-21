"""Persistent state stays in one application directory."""

from __future__ import annotations

from ripart.common.creds import CredentialStore
from ripart.common.storage import state_path
from ripart.cli_extractors import ExtractionUI, save_listed_cards


def test_state_path_uses_configured_application_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("RIPART_HOME", str(tmp_path / "ripart-state"))

    assert state_path("saucepan-token") == tmp_path / "ripart-state" / "saucepan-token"


def test_credential_store_creates_parent_directory(tmp_path):
    current = tmp_path / "ripart-state" / "saucepan-token"

    store = CredentialStore(current, empty="")

    store.store("rotated")
    assert current.read_text(encoding="utf-8") == "rotated"


def test_listed_card_saver_skips_existing_and_continues_after_errors(monkeypatch, tmp_path):
    messages: list[str] = []
    ui = ExtractionUI(
        library_dir=tmp_path,
        print=messages.append,
        error=messages.append,
        ok=messages.append,
        no=messages.append,
        field=lambda _label, _value: None,
        path=lambda _label, _value: None,
        duration=lambda _seconds: "0s",
    )
    (tmp_path / "already.png").touch()
    saved: list[str] = []
    monkeypatch.setattr(
        "ripart.cli_extractors.save_to_library",
        lambda _directory, identifier, _result: (saved.append(identifier), {"png": str(tmp_path / f"{identifier}.png")})[1],
    )

    def extract(item):
        if item["id"] == "broken":
            raise RuntimeError("unavailable")
        return {"characterId": item["id"]}

    save_listed_cards(
        ui,
        [{"id": "already"}, {"id": "broken"}, {"id": "new"}],
        item_id=lambda item: item["id"],
        item_name=lambda item: item["id"],
        extract=extract,
    )

    assert saved == ["new"]
    assert any("broken: unavailable" in message for message in messages)
    assert any("saved [bold]1[/] card(s); skipped [bold]1[/]" in message for message in messages)
