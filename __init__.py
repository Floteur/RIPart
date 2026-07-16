"""RIPart — rip characters & lorebooks from JanitorAI and Saucepan.

Use it as a library:

    >>> import ripart
    >>> ripart.login()
    >>> result = ripart.extract("https://janitorai.com/characters/<uuid>_name")
    >>> result["savedPath"]

or as a CLI (``rip --help`` / ``python -m ripart``). See :mod:`ripart.api` for
the full high-level API, and ``ripart.saucepan`` for lower-level Saucepan calls.
"""

from __future__ import annotations

from .api import (
    DEFAULT_OUTPUT_DIR,
    SaucepanError,
    extract,
    import_session,
    inspect,
    is_logged_in,
    login,
    recent,
    save,
    saucepan,
)

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("ripart")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0.dev0"

__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "SaucepanError",
    "__version__",
    "extract",
    "import_session",
    "inspect",
    "is_logged_in",
    "login",
    "recent",
    "save",
    "saucepan",
]
