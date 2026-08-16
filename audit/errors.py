"""What can go wrong, separated by what the reader should do about it.

The distinction this hierarchy exists to preserve is between *your numbers
disagree* and *I could not perform the check*. They call for opposite responses
-- one is a defect in the pricing, the other is a defect in the harness or its
inputs -- and collapsing them makes a broken tool look like a billing error.

So a disagreement is never an exception. It is a result: a ``Comparison`` with a
mismatch outcome, reported and counted. Everything here means the audit did not
happen, and every one of them exits 2.
"""

from __future__ import annotations


class AuditError(Exception):
    """The audit could not be completed. Exit code 2."""


class PortalError(AuditError):
    """The portal did not answer the way it was captured answering.

    Carries which endpoint and which step, because the first question after a
    failure is always whether the session expired or the flow moved, and those
    have different answers: re-login versus re-capture.
    """

    def __init__(self, message: str, *, endpoint: str = "", step: str = "") -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.step = step


class AccountError(AuditError):
    """The selected managed account profile cannot price this cycle.

    Raised rather than guessed when a profile is missing or a statement falls
    outside its effective-dated epochs.
    """
