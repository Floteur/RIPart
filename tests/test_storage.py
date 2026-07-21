"""Persistent state stays in one application directory."""

from __future__ import annotations

from ripart.common.creds import CredentialStore
from ripart.common.storage import state_path


def test_state_path_uses_configured_application_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("RIPART_HOME", str(tmp_path / "ripart-state"))

    assert state_path("saucepan-token") == tmp_path / "ripart-state" / "saucepan-token"


def test_credential_store_creates_parent_and_migrates_legacy_file(tmp_path):
    legacy = tmp_path / ".saucepan-token"
    legacy.write_text("secret", encoding="utf-8")
    current = tmp_path / "ripart-state" / "saucepan-token"

    store = CredentialStore(current, empty="", legacy_path=legacy)

    assert store.load() == "secret"
    assert current.read_text(encoding="utf-8") == "secret"
    assert not legacy.exists()

    store.store("rotated")
    assert current.read_text(encoding="utf-8") == "rotated"
