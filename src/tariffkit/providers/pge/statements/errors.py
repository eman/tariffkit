"""Errors raised while importing PG&E statements."""

from __future__ import annotations

from collections.abc import Iterable

from tariffkit.errors import TariffKitError


class StatementError(TariffKitError):
    """A statement could not be read or did not pass its self-check."""


class StatementAmbiguityError(StatementError):
    """The statement does not provide one-to-one agreement evidence."""

    def __init__(self, message: str, *, diagnostics: Iterable[str] = ()) -> None:
        self.diagnostics = tuple(diagnostics)
        detail = "; ".join(self.diagnostics)
        super().__init__(f"{message}: {detail}" if detail else message)
