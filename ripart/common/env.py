"""Shared environment loading for RIPart (used by both the bot and publisher)."""

from __future__ import annotations

import io
import os
from pathlib import Path

from dotenv import dotenv_values, find_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def env_paths() -> tuple[Path, ...]:
    paths: list[Path] = []
    if found := find_dotenv(usecwd=True):
        paths.append(Path(found))
    paths.append(_PROJECT_ROOT / ".env")
    return tuple(dict.fromkeys(path.resolve() for path in paths if path.is_file()))


def load_env() -> None:
    if os.environ.get("_RIPART_ENV_LOADED"):
        return
    os.environ["_RIPART_ENV_LOADED"] = "1"
    for path in env_paths():
        assignments = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        for key, value in dotenv_values(stream=io.StringIO(assignments)).items():
            if key and value is not None:
                os.environ.setdefault(key, value)
