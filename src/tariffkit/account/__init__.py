"""The account: what an agreement was, and when.

A profile is a dated history of :class:`~tariffkit.config.Config` snapshots
plus what the utility's own statements said, so a bill prices with the settings
in force over its own days. Reading and writing one is the caller's business --
the command line keeps a file, Home Assistant keeps a config entry -- so this
package never touches a disk.
"""

from .errors import AccountError
from .model import (
    SCHEMA_VERSION,
    AccountEpoch,
    AccountObservation,
    AccountProfile,
    MeterSource,
    MeterSources,
    ObservedAgreement,
    mask_account_digits,
)
from .rates import AccountRateEngine

__all__ = [
    "SCHEMA_VERSION",
    "AccountEpoch",
    "AccountError",
    "AccountObservation",
    "AccountProfile",
    "AccountRateEngine",
    "MeterSource",
    "MeterSources",
    "ObservedAgreement",
    "mask_account_digits",
]
