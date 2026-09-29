"""The export credit bank, folded from a run of billing cycles.

The fold lives in the library, :mod:`tariffkit.billing.bank`, so the CLI and
this integration carry the same bank for the same account. Re-exported here for
the coordinator and for anything that imported it from the integration.
"""

from __future__ import annotations

from tariffkit.billing.bank import BankState, fold

__all__ = ["BankState", "fold"]
