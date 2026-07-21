"""Locations for RIPart's persistent local state."""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    """Return RIPart's state directory without creating it."""
    if configured := os.environ.get("RIPART_HOME"):
        return Path(configured).expanduser()
    if base := os.environ.get("XDG_STATE_HOME"):
        return Path(base).expanduser() / "ripart"
    return Path.home() / ".local" / "state" / "ripart"


def state_path(name: str) -> Path:
    """Return a named path inside the RIPart state directory."""
    return state_dir() / name
