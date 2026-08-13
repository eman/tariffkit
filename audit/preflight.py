"""Everything an end-to-end run needs, checked before it starts.

A run touches four things that can each be absent or wrong -- credentials, the
portal, the meter database, and the account's own history -- and a failure in
any of them surfaces hundreds of lines later as something that looks like a
billing discrepancy. Checking them up front costs one command and turns "the
audit says I was overcharged $300" back into "InfluxDB is not reachable".

Each check reports rather than raises, so one run tells you everything that is
missing instead of one thing at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    #: False when the run can still proceed without it, with reduced coverage.
    required: bool = True

    @property
    def mark(self) -> str:
        if self.ok:
            return "ok"
        return "MISSING" if self.required else "degraded"


def _credentials() -> Check:
    from nem_rates.sources.pge import PgeSettings

    try:
        settings = PgeSettings.load()
    except Exception as exc:
        return Check("PG&E credentials", False, str(exc)[:120])

    missing = [
        name
        for name, value in (
            ("PGE_USERNAME", settings.username),
            ("PGE_PASSWORD", settings.password),
            ("PGE_ACCOUNT_ID", settings.account_id),
        )
        if not value
    ]
    if missing:
        return Check("PG&E credentials", False, f"set {', '.join(missing)} in .env")

    # Device cookies are not credentials and not optional in practice: without
    # them the portal does not recognise the machine and asks to verify it,
    # which reads as an MFA prompt on an account that has no MFA.
    if not (settings.browser_cookie and settings.validation_cookie):
        return Check(
            "PG&E credentials",
            False,
            "set PGE_BROWSER_COOKIE and PGE_VALIDATION_COOKIE; without them the "
            "portal treats this as a new device and asks to verify it. See pge/PORTAL.md",
        )
    return Check("PG&E credentials", True, f"{settings.username[:3]}…, account configured")


def _portal() -> Check:
    from nem_rates.sources.pge import PgeSession, PgeSettings

    try:
        settings = PgeSettings.load()
        with PgeSession(settings) as session:
            session.login()
            bills = session.bill_history()
    except Exception as exc:
        return Check("portal", False, str(exc)[:160])
    if not bills:
        return Check("portal", False, "signed in, but the portal listed no statements")
    return Check("portal", True, f"signed in; {len(bills)} statements listed")


def _influx() -> Check:
    from nem_rates.sources.influx import InfluxSettings, read_counters
    from nem_rates.timeutil import PACIFIC

    try:
        settings = InfluxSettings.load()
    except Exception as exc:
        return Check("meter data (InfluxDB)", False, str(exc)[:120])

    from datetime import datetime

    end = datetime.now(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        readings = read_counters(settings, end - timedelta(days=2), end)
    except Exception as exc:
        return Check("meter data (InfluxDB)", False, str(exc)[:160])
    if not readings:
        return Check("meter data (InfluxDB)", False, "reachable, but no samples in the last 2 days")
    return Check("meter data (InfluxDB)", True, f"{len(readings)} intervals in the last 2 days")


def _account(path: Path) -> Check:
    from .account import AccountHistory

    try:
        history = AccountHistory.from_toml(path)
    except Exception as exc:
        return Check(f"account history ({path})", False, str(exc)[:160])
    if not history.epochs:
        return Check(f"account history ({path})", False, "no [[epoch]] blocks")
    spans = ", ".join(f"{e.start} {e.overrides.get('tariff', '?')}" for e in history.epochs)
    return Check(f"account history ({path})", True, spans)


def _recognition() -> Check:
    from .statements.ocr import available

    if available():
        return Check("page recognition (tesseract, poppler)", True, "installed")
    return Check(
        "page recognition (tesseract, poppler)",
        False,
        "brew install tesseract poppler -- without it, statements before "
        "November 2025 cannot be read at all, since they carry no text layer",
        required=False,
    )


def _rate_data(oldest: date) -> Check:
    """Whether a tariff snapshot exists back to the oldest cycle to be priced."""
    from nem_rates.data import versioned
    from nem_rates.errors import DataError

    try:
        versioned.load("tax/ca_energy_resources", oldest)
    except DataError as exc:
        return Check("rate data", False, str(exc)[:160])
    return Check("rate data", True, f"covers {oldest}")


def run_checks(*, account: Path, oldest: date, contact: bool = True) -> list[Check]:
    """Every prerequisite, in the order a run needs them."""
    checks = [_credentials(), _account(account), _recognition(), _rate_data(oldest)]
    if contact:
        # Ordered after the local checks so a missing .env is reported without
        # a network round trip, and so a portal failure is never the first
        # thing blamed when the real problem is configuration.
        checks.extend([_portal(), _influx()])
    return checks
