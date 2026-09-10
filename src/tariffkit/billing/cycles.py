"""Which billing cycle a day falls in, and on what authority.

A meter-read day is a guess, and a bad one: PG&E reads on business days, so a
real account's cycles open on the 29th, the 30th, the 1st and the 3rd in
consecutive months. Any fixed day of the month is therefore wrong for most
cycles -- fine for a rough month-to-date figure, not fine for anything that
claims to track a bill.

Statements say exactly where the boundaries fell. Every answer here carries
which of the two it came from, so a figure that does not match a bill says why
before the reader has to guess.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from .models import BillingPeriod

if TYPE_CHECKING:
    # `tariffkit.account` builds on `BillingPeriod`, so importing it here at
    # runtime would close a cycle. Only the annotation is needed.
    from ..account import AccountProfile

#: How long after a statement's period ends its evidence still fixes the
#: current cycle's start. A real PG&E cycle runs 27 to 33 days, so a wider gap
#: means at least one statement has been issued that the account never imported
#: and the next boundary is no longer derivable from what it knows.
#:
#: Measured from the day the derived cycle opened, so the bound is one less than
#: the longest real cycle: at 32 days elapsed the cycle-to-date spans 33 days,
#: and anything beyond that would report a period no bill could have.
STALE_EVIDENCE = timedelta(days=32)


@dataclass(frozen=True, slots=True)
class Cycle:
    """When a billing cycle opened, and how that was established."""

    start: date
    #: ``statement`` when real evidence fixed it, ``day_of_month`` when a
    #: configured meter-read day was used, ``calendar_month`` when neither was
    #: available. Carried so that a figure which does not match a bill says why
    #: before the reader has to guess; how it is worded is the caller's.
    source: str


def statement_periods(profile: AccountProfile) -> tuple[BillingPeriod, ...]:
    """Billing periods the account holds statement evidence for, oldest first.

    One per statement, not one per agreement. A cycle that changed service
    agreement partway -- which is exactly what interconnecting solar does --
    prints two agreement blocks inside one billing period, and it is the period
    the utility bills that a cycle-to-date figure has to follow.
    """
    found: list[BillingPeriod] = []
    for observation in profile.observations:
        spans = [agreement.period for agreement in observation.agreements]
        if not spans:
            continue
        found.append(BillingPeriod(min(s.start for s in spans), max(s.end for s in spans)))
    return tuple(sorted(found, key=lambda period: period.start))


def known_periods(profile: AccountProfile) -> tuple[BillingPeriod, ...]:
    """Every boundary the account knows, with statements outranking the portal.

    Two sources say where cycles fell and they answer slightly different
    questions. A statement is one page, so a cycle whose service agreement
    changed partway -- interconnecting solar does exactly this -- is one period
    on it. The portal lists a bill per agreement, so the same cycle comes back
    as two. Following the utility's own page is what a cycle-to-date figure has
    to do, so where they overlap the statement wins.

    The portal covers the rest, which is most of it: it lists three years
    without a PDF to parse, and an account that has imported no statements at
    all -- Home Assistant's, until someone pastes one in -- would otherwise be
    guessing at a meter-read day.
    """
    statements = statement_periods(profile)
    kept = [
        period
        for period in profile.billing_periods
        if not any(period.start <= other.end and other.start <= period.end for other in statements)
    ]
    return tuple(sorted([*statements, *kept], key=lambda period: period.start))


def _by_day_of_month(day: date, start_day: int) -> Cycle:
    """Fall back to a fixed meter-read day, or to the calendar month.

    Months are not all the same length, so a 31st-of-the-month read clamps to
    the 30th in April and the 28th in February rather than failing or skipping
    a cycle.
    """
    if not start_day:
        return Cycle(day.replace(day=1), "calendar_month")
    anchor = min(start_day, monthrange(day.year, day.month)[1])
    if day.day >= anchor:
        return Cycle(day.replace(day=anchor), "day_of_month")
    previous = day.replace(day=1) - timedelta(days=1)
    anchor = min(start_day, monthrange(previous.year, previous.month)[1])
    return Cycle(previous.replace(day=anchor), "day_of_month")


def resolve_cycle(day: date, start_day: int, periods: Sequence[BillingPeriod] = ()) -> Cycle:
    """Find the billing cycle containing ``day``, preferring real evidence.

    Cycles are contiguous -- each period begins the day after the last one
    ended -- so the open cycle's start follows from the most recent statement,
    without waiting for the statement that will close it.

    ``start_day`` remains the fallback, because an account configured by hand
    has no statement evidence at all.
    """
    for period in reversed(periods):
        if period.start <= day <= period.end:
            return Cycle(period.start, "statement")
    latest = max((period.end for period in periods), default=None)
    if latest is not None and latest < day:
        opened = latest + timedelta(days=1)
        if day - opened <= STALE_EVIDENCE:
            return Cycle(opened, "statement")
    return _by_day_of_month(day, start_day)


def cycle_start(day: date, start_day: int, periods: Sequence[BillingPeriod] = ()) -> date:
    """The cycle's first day; see :func:`resolve_cycle` for how it is chosen."""
    return resolve_cycle(day, start_day, periods).start
