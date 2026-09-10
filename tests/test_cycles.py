"""Which billing cycle a day falls in.

A meter-read day is a guess and a statement is evidence, so these are mostly
about the second beating the first -- and about the fallback still being sane
where there is no evidence at all.
"""

from __future__ import annotations

from datetime import date

import pytest

from tariffkit.account import AccountEpoch, AccountProfile
from tariffkit.account.model import AccountObservation, ObservedAgreement
from tariffkit.billing import (
    BillingPeriod,
    Cycle,
    cycle_start,
    known_periods,
    resolve_cycle,
    statement_periods,
)
from tariffkit.config import Config


def _period(start: tuple[int, int, int], end: tuple[int, int, int]) -> BillingPeriod:
    return BillingPeriod(date(*start), date(*end))


@pytest.mark.parametrize(
    ("day", "start_day", "expected"),
    [
        (date(2026, 8, 24), 0, date(2026, 8, 1)),
        (date(2026, 8, 24), 12, date(2026, 8, 12)),
        (date(2026, 8, 3), 12, date(2026, 7, 12)),
        (date(2026, 8, 12), 12, date(2026, 8, 12)),
        # A read day past the end of a short month clamps rather than skipping
        # a cycle or raising.
        (date(2026, 3, 15), 31, date(2026, 2, 28)),
        (date(2026, 5, 3), 31, date(2026, 4, 30)),
    ],
)
def test_cycle_start_clamps_to_the_month(day: date, start_day: int, expected: date) -> None:
    """The fallback, used when no statement evidence exists."""
    assert cycle_start(day, start_day) == expected


def test_statement_evidence_beats_a_guessed_meter_read_day() -> None:
    """Real cycles do not open on a fixed day, so evidence wins where it exists.

    PG&E reads on business days, so consecutive cycles on one real account
    opened on the 29th, the 30th, the 1st and the 3rd. Any fixed day of the
    month is therefore wrong for most of them.
    """
    periods = [_period((2026, 6, 1), (2026, 6, 29)), _period((2026, 6, 30), (2026, 7, 28))]

    # Inside a billed cycle: that cycle's own start, exactly.
    assert resolve_cycle(date(2026, 7, 10), 30, periods) == Cycle(date(2026, 6, 30), "statement")

    # After the last statement: cycles are contiguous, so the open one began
    # the day after it ended -- derivable without waiting to be billed.
    assert resolve_cycle(date(2026, 8, 24), 30, periods) == Cycle(date(2026, 7, 29), "statement")

    # The guess would have been a day out, and the calendar month three.
    assert cycle_start(date(2026, 8, 24), 30) == date(2026, 7, 30)
    assert cycle_start(date(2026, 8, 24), 0) == date(2026, 8, 1)


def test_stale_evidence_falls_back_rather_than_inventing_a_long_cycle() -> None:
    """Evidence older than a cycle cannot fix the current boundary.

    A statement has been issued that the account never imported, so the next
    boundary is not derivable. Trusting the old one would report a 90-day
    "cycle" and charge the Base Services Charge for every day of it.
    """
    periods = [_period((2026, 6, 30), (2026, 7, 28))]
    assert resolve_cycle(date(2026, 8, 24), 30, periods).source == "statement"
    stale = resolve_cycle(date(2026, 10, 1), 30, periods)
    assert stale.source == "day_of_month"
    assert stale.start == date(2026, 9, 30)


def test_evidence_never_implies_a_cycle_longer_than_a_real_one() -> None:
    """A cycle runs 27-33 days; the staleness bound must refuse before 34."""
    periods = [_period((2026, 6, 30), (2026, 7, 28))]
    spans = {}
    for day in (date(2026, 8, 29), date(2026, 8, 30), date(2026, 8, 31), date(2026, 9, 1)):
        cycle = resolve_cycle(day, 30, periods)
        spans[day] = ((day - cycle.start).days + 1, cycle.source)
    assert spans[date(2026, 8, 29)] == (32, "statement")
    assert spans[date(2026, 8, 30)] == (33, "statement")
    # Beyond a real cycle, so the evidence is stale and it says so.
    assert spans[date(2026, 8, 31)][1] == "day_of_month"
    assert all(span <= 33 for span, source in spans.values() if source == "statement")


def test_statement_periods_follow_the_bill_not_the_agreement() -> None:
    """A cycle split by interconnection is one billing period, not two."""
    profile = AccountProfile(
        (AccountEpoch(date(2026, 1, 1), Config(tariff="E-ELEC")),),
        name="split",
        observations=(
            AccountObservation(
                agreements=(
                    ObservedAgreement(
                        provider="pge",
                        statement_date=date(2026, 7, 7),
                        period=_period((2026, 6, 1), (2026, 6, 2)),
                        tariff="EV2-A",
                    ),
                    ObservedAgreement(
                        provider="pge",
                        statement_date=date(2026, 7, 7),
                        period=_period((2026, 6, 3), (2026, 6, 29)),
                        tariff="E-ELEC",
                    ),
                ),
            ),
        ),
    )

    (period,) = statement_periods(profile)

    assert (period.start, period.end) == (date(2026, 6, 1), date(2026, 6, 29))


def _observed(period: BillingPeriod, *, tariff: str = "E-ELEC") -> AccountObservation:
    return AccountObservation(
        agreements=(
            ObservedAgreement(
                provider="pge",
                statement_date=period.end,
                period=period,
                tariff=tariff,
            ),
        )
    )


class TestKnownPeriods:
    """Two sources of boundaries, answering slightly different questions."""

    @staticmethod
    def _profile(**kwargs: object) -> AccountProfile:
        return AccountProfile((AccountEpoch(date(2026, 1, 1), Config()),), **kwargs)  # type: ignore[arg-type]

    def test_the_portal_fills_in_where_no_statement_was_imported(self) -> None:
        """Which is the whole point: Home Assistant holds no portal credentials."""
        profile = self._profile(
            billing_periods=(
                _period((2026, 6, 30), (2026, 7, 28)),
                _period((2026, 7, 29), (2026, 8, 27)),
            )
        )

        assert resolve_cycle(date(2026, 9, 9), 0, known_periods(profile)) == Cycle(
            date(2026, 8, 28), "statement"
        )

    def test_a_statement_outranks_the_portal_where_they_overlap(self) -> None:
        """A cycle split by interconnection is one page and two agreements.

        The portal lists a bill per agreement, so it reports the June cycle
        twice; the statement prints it once, and a cycle-to-date figure has to
        follow the page that was billed.
        """
        profile = self._profile(
            observations=(_observed(_period((2026, 6, 1), (2026, 6, 29))),),
            billing_periods=(
                _period((2026, 6, 1), (2026, 6, 2)),
                _period((2026, 6, 3), (2026, 6, 29)),
                _period((2026, 6, 30), (2026, 7, 28)),
            ),
        )

        assert known_periods(profile) == (
            _period((2026, 6, 1), (2026, 6, 29)),
            _period((2026, 6, 30), (2026, 7, 28)),
        )
        assert resolve_cycle(date(2026, 6, 15), 0, known_periods(profile)).start == date(2026, 6, 1)

    def test_neither_source_is_no_periods_rather_than_an_error(self) -> None:
        assert known_periods(self._profile()) == ()
