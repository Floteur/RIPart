"""RIPart — rip characters & lorebooks from JanitorAI, Saucepan, clank.world,
spicychat.ai, chub.ai, character-tavern, and any public Tavern card file.

Use it as a library:

    >>> import ripart
    >>> ripart.login()
    >>> result = ripart.extract("https://janitorai.com/characters/<uuid>_name")
    >>> result["savedPath"]

or as a CLI (``rip --help`` / ``python -m ripart``). See :mod:`ripart.api` for
the full high-level API, and ``ripart.providers.saucepan`` for lower-level calls.
"""

from __future__ import annotations

from .api import (
    DEFAULT_OUTPUT_DIR,
    ChubError,
    ClankError,
    SaucepanError,
    SpicyChatError,
    TavernCardError,
    chub,
    clank,
    extract,
    import_session,
    inspect,
    is_logged_in,
    login,
    recent,
    save,
    saucepan,
    spicychat,
    tavern,
)

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("ripart")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0.dev0"

__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "ChubError",
    "ClankError",
    "SaucepanError",
    "SpicyChatError",
    "TavernCardError",
    "__version__",
    "chub",
    "clank",
    "extract",
    "import_session",
    "inspect",
    "is_logged_in",
    "login",
    "recent",
    "save",
    "saucepan",
    "spicychat",
    "tavern",
]
