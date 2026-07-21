"""Provider-agnostic building blocks shared by every RIPart provider.

- :mod:`ripart.common.errors` — the ``RipError`` base exception.
- :mod:`ripart.common.http` — a base-URL-scoped pooled HTTP client with retry
  and wire tracing.
- :mod:`ripart.common.creds` — file-persisted credentials with a thread override.
- :mod:`ripart.common.echo` — echo-proxy leak parsing.
- :mod:`ripart.common.avatar` — avatar download → ``data:`` URI.
- :mod:`ripart.common.text` — text/file utilities.
- :mod:`ripart.common.cards` — Tavern V2/V3 card + library assembly.
"""

from __future__ import annotations

from .errors import RipError

__all__ = ["RipError"]
