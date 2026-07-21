"""Base exception shared by every provider client."""

from __future__ import annotations


class RipError(Exception):
    """User-facing provider failure; carries an optional HTTP-ish status code.

    ``partial`` holds any text streamed before a generation failed or timed out,
    so a cut-off leak can still be salvaged rather than lost (see the Saucepan
    leak path). Each provider subclasses this (``SaucepanError``/``ClankError``)
    so callers can still catch a provider-specific type.
    """

    def __init__(
        self, message: str, status: int | None = None, partial: str = ""
    ) -> None:
        super().__init__(message)
        self.status = status
        self.partial = partial
