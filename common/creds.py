"""Persisted credentials with a per-thread override.

Providers persist credentials in RIPart's application-state directory and reuse
them for every call. A ``threading.local`` override lets several accounts drive
one process concurrently (each worker thread carries its own credential). The
value type differs — a plain string vs a dict — so :class:`CredentialStore` is
parameterised by ``loads``/``dumps`` (raw text for a token, JSON for a session).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class CredentialStore:
    """A file-persisted credential with an in-memory cache and thread override."""

    def __init__(
        self,
        path: Path,
        *,
        empty: Any,
        loads: Callable[[str], Any] = lambda s: s.strip(),
        dumps: Callable[[Any], str] = lambda v: str(v),
        legacy_path: Path | None = None,
    ) -> None:
        self._path = Path(path)
        self._empty = empty
        self._loads = loads
        self._dumps = dumps
        self._legacy_path = Path(legacy_path) if legacy_path else None
        self._value: Any = None  # None = not yet loaded from disk
        self._override = threading.local()

    def active(self) -> Any:
        """The credential for the calling thread: its override, else the global."""
        override = getattr(self._override, "value", None)
        return override if override else self.load()

    @contextmanager
    def use(self, value: Any):
        """Run a block with ``value`` as the active credential for this thread only.

        Nesting is supported (the previous value is restored on exit). A falsy
        value falls back to the global credential for the duration of the block.
        """
        prev = getattr(self._override, "value", None)
        self._override.value = value
        try:
            yield
        finally:
            self._override.value = prev

    def load(self) -> Any:
        """Read the persisted credential into memory (called on first use)."""
        if self._value is None:
            if not self._path.exists() and self._legacy_path and self._legacy_path.exists():
                self._path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self._legacy_path.replace(self._path)
                except OSError:
                    # Use an unmigrated credential rather than logging the user out.
                    self._path = self._legacy_path
            if self._path.exists():
                # Tighten perms on an existing (possibly world-readable) file.
                try:
                    if (self._path.stat().st_mode & 0o077) != 0:
                        os.chmod(self._path, 0o600)
                except OSError:
                    pass
                try:
                    self._value = self._loads(
                        self._path.read_text(encoding="utf-8").strip()
                    )
                except (ValueError, OSError):
                    self._value = self._empty
            else:
                self._value = self._empty
        return self._value

    def store(self, value: Any) -> None:
        """Store a credential both in memory and on disk (owner-only file perms)."""
        self._value = value
        if value:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(self._dumps(value), encoding="utf-8")
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

    def persist_active(self, value: Any) -> None:
        """Update the active credential in place after an in-flight refresh.

        When a thread override is active (a worker running under :meth:`use`),
        the new value replaces that override in memory only — never touching the
        shared on-disk credential. Otherwise it is stored globally (memory +
        disk). This lets token-rotation flows save a freshly minted token without
        one account's refresh clobbering another's persisted credential.
        """
        if getattr(self._override, "value", None) is not None:
            self._override.value = value
        else:
            self.store(value)

    def clear(self) -> None:
        """Forget the credential (log out)."""
        self._value = self._empty
        self._path.unlink(missing_ok=True)
