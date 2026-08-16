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

from tariffkit.account import AccountProfile


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
    from tariffkit.sources.pge import PgeSettings

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
    from tariffkit.sources.pge import PgeSession, PgeSettings

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
    from tariffkit.sources.influx import InfluxSettings, read_counters
    from tariffkit.timeutil import PACIFIC

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


def _profile_name(account: str | None) -> str | None:
    if account is not None:
        return account
    from tariffkit.account import configured_profile_name

    return configured_profile_name()


def _load_profile(account: str | None) -> tuple[str, AccountProfile]:
    from tariffkit.account import NamedProfileRepository

    name = _profile_name(account)
    if name is None:
        raise ValueError("select a named account profile with --account or configuration")
    return name, NamedProfileRepository().load(name)


def _account(account: str | None) -> Check:
    try:
        name, profile = _load_profile(account)
    except Exception as exc:
        return Check("account profile", False, str(exc)[:160])
    spans = ", ".join(f"{epoch.effective} {epoch.config.tariff}" for epoch in profile.epochs)
    return Check(f"account profile ({name})", True, spans)


def _recognition() -> Check:
    from tariffkit.providers.pge.statements.ocr import available

    if available():
        return Check("page recognition (tesseract, poppler)", True, "installed")
    return Check(
        "page recognition (tesseract, poppler)",
        False,
        "brew install tesseract poppler -- without it, statements before "
        "November 2025 cannot be read at all, since they carry no text layer",
        required=False,
    )


def _rate_data(account: str | None, oldest: date) -> Check:
    """Whether every schedule the account was on can be priced back to ``oldest``.

    Checking one dataset was not enough. The tax vintage spans the whole window
    on any account, so it passed while the schedule actually in force had no
    snapshot -- which then surfaced mid-run as a priced-but-wrong cycle rather
    than as missing data. Each epoch is asked for the tariff it names, on the
    later of its own start and the oldest cycle wanted.
    """
    from tariffkit.data import versioned
    from tariffkit.errors import DataError

    problems: list[str] = []
    try:
        versioned.load("tax/ca_energy_resources", oldest)
    except DataError as exc:
        problems.append(str(exc)[:100])

    checked: list[str] = []
    try:
        _name, profile = _load_profile(account)
    except Exception:
        # Already reported by its own check; nothing to add here.
        profile = None

    epochs = profile.epochs if profile else ()
    for epoch in epochs:
        # Epochs that ended before the window are not priced, so a missing
        # snapshot for one is not a problem this run has.
        later = [e.effective for e in epochs if e.effective > epoch.effective]
        if later and min(later) <= oldest:
            continue
        assert profile is not None
        when = max(epoch.effective, oldest)
        config = epoch.config
        try:
            versioned.load(f"tariff/{config.utility.data_slug}/{_slug(config.tariff)}", when)
            checked.append(config.tariff)
        except DataError as exc:
            problems.append(f"{config.tariff}: {str(exc)[:80]}")

    if problems:
        return Check("rate data", False, "; ".join(problems)[:200])
    return Check("rate data", True, f"covers {oldest} for {', '.join(dict.fromkeys(checked))}")


def _slug(tariff: str) -> str:
    return tariff.lower().replace("-", "")


def _cca_card(account: str | None, oldest: date) -> Check:
    """Whether the vendored CCA rate card is anywhere near the cycles priced.

    Its own check because it is not an error and cannot be fixed by vendoring
    when the provider publishes no archive -- MCE files no advice letters, so
    superseded vintages are simply unavailable. Worth knowing before a run
    rather than inferring it from a thirty-cent generation gap.
    """
    from tariffkit.cca import load_rate_card
    from tariffkit.errors import DataError

    try:
        _name, profile = _load_profile(account)
    except Exception:
        return Check("CCA rate card", True, "no account profile to check against")

    stale: list[str] = []
    for epoch in profile.epochs:
        cca = epoch.config.cca
        if cca is None or cca.rate_card is None:
            continue
        try:
            card = load_rate_card(cca.rate_card, max(epoch.effective, oldest))
        except DataError as exc:
            return Check("CCA rate card", False, str(exc)[:160])
        age = (max(epoch.effective, oldest) - card.effective).days
        if age > 400:
            stale.append(
                f"{cca.rate_card.upper()} card is {age} days older than the cycles it prices"
            )
    if stale:
        return Check("CCA rate card", False, "; ".join(dict.fromkeys(stale))[:200], required=False)
    return Check("CCA rate card", True, "current for the window")


def run_checks(*, account: str | None, oldest: date, contact: bool = True) -> list[Check]:
    """Every prerequisite, in the order a run needs them."""
    checks = [
        _credentials(),
        _account(account),
        _recognition(),
        _rate_data(account, oldest),
        _cca_card(account, oldest),
    ]
    if contact:
        # Ordered after the local checks so a missing .env is reported without
        # a network round trip, and so a portal failure is never the first
        # thing blamed when the real problem is configuration.
        checks.extend([_portal(), _influx()])
    return checks
