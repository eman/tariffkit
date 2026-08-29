"""The export credit bank folded from a run of billing cycles."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from custom_components.tariffkit import backfill, bank
from custom_components.tariffkit.energy import price

from tariffkit import Config
from tariffkit.account import AccountEpoch, AccountProfile
from tariffkit.billing import BillingPeriod, IntervalReading, run_ledger
from tariffkit.timeutil import PACIFIC

PTO = date(2026, 6, 3)


def _profile(**kwargs: object) -> AccountProfile:
    config = Config(tariff="E-ELEC", pto_date=PTO, **kwargs)  # type: ignore[arg-type]
    return AccountProfile((AccountEpoch(date(2026, 1, 1), config),), name="probe")


def _cycle_bill(profile: AccountProfile, opens: date, closes: date, exported: float) -> object:
    readings = []
    day = opens
    while day <= closes:
        readings += [
            IntervalReading(
                datetime(day.year, day.month, day.day, hour, tzinfo=PACIFIC),
                imported=0.2,
                exported=exported,
            )
            for hour in range(24)
        ]
        day += timedelta(days=1)
    bill, reason = price(profile, readings, BillingPeriod(opens, closes))
    assert bill is not None, reason
    return bill


def test_the_bank_carries_between_cycles() -> None:
    """Heavy export earns more credit than a cycle can spend; the rest banks."""
    profile = _profile()
    bills = [
        _cycle_bill(profile, date(2026, 6, 1), date(2026, 6, 30), exported=2.0),
        _cycle_bill(profile, date(2026, 7, 1), date(2026, 7, 31), exported=2.0),
    ]
    state = bank.fold(profile, bills)

    assert state.cycles == 2
    assert state.period == (date(2026, 6, 1), date(2026, 7, 31))
    assert state.balance.total > 0
    assert state.trustworthy
    assert state.warnings == ()
    # And it agrees with folding the ledger directly.
    assert state.balance.total == pytest.approx(run_ledger(bills).entries[-1].closing.total)


def test_a_gap_between_cycles_is_refused_not_folded_over() -> None:
    """`run_ledger` will not check for this; the caller has to.

    Folding across a missing cycle does not merely lose it -- the credits it
    earned and spent are absent from the arithmetic entirely, so the balance
    reported never existed.
    """
    profile = _profile()
    bills = [
        _cycle_bill(profile, date(2026, 6, 1), date(2026, 6, 30), exported=2.0),
        # July is missing.
        _cycle_bill(profile, date(2026, 8, 1), date(2026, 8, 31), exported=2.0),
    ]
    state = bank.fold(profile, bills)

    assert not state.trustworthy
    assert any("cannot be carried across a gap" in w for w in state.warnings)
    assert any("2026-06-30" in w and "2026-08-01" in w for w in state.warnings)


def test_no_cycles_means_no_bank_rather_than_zero() -> None:
    """Zero is a balance. Nothing priced is not a balance."""
    state = bank.fold(_profile(), [])
    assert state.cycles == 0
    assert not state.trustworthy
    assert "no priced cycles" in state.warnings


def test_a_bank_folded_from_the_pto_cycle_needs_no_opening_balance() -> None:
    """The reason that cycle is where a backfill starts.

    Nothing before Permission To Operate earns anything, so there is no earlier
    balance to carry and the fold is self-contained.
    """
    profile = _profile()
    first = _cycle_bill(profile, date(2026, 6, 1), date(2026, 6, 30), exported=2.0)
    state = bank.fold(profile, [first])
    assert run_ledger([first]).entries[0].opening.total == 0.0
    assert state.balance.total == pytest.approx(run_ledger([first]).entries[-1].closing.total)


def test_the_backfill_bills_fold_straight_into_a_bank() -> None:
    """The two halves meet: what the action returns is what the bank consumes."""
    profile = _profile()
    readings = []
    day = date(2026, 6, 1)
    while day <= date(2026, 7, 31):
        readings += [
            IntervalReading(
                datetime(day.year, day.month, day.day, hour, tzinfo=PACIFIC),
                imported=0.2,
                exported=2.0,
            )
            for hour in range(24)
        ]
        day += timedelta(days=1)

    result = backfill.build(profile, readings, date(2026, 6, 1), date(2026, 7, 31), 1)
    state = bank.fold(profile, result.bills)
    assert state.cycles == len(result.bills) == 2
    assert state.trustworthy
    assert state.balance.total > 0


def _cca_profile() -> AccountProfile:
    from tariffkit.config import CcaConfig
    from tariffkit.models import Supplier

    config = Config(
        tariff="E-ELEC",
        pto_date=PTO,
        supplier=Supplier.CCA,
        cca=CcaConfig(name="MCE", rate_card="MCE", option="light_green", pcia_vintage=2011),
        baseline_territory="X",
    )
    return AccountProfile((AccountEpoch(date(2026, 1, 1), config),), name="probe")


def test_a_cca_account_holds_two_banks_that_never_settle_together() -> None:
    """A statement prints them on separate pages, on unrelated calendars.

    PG&E keeps "Energy Delivered Credits" and "Bonus Credits"; the CCA keeps its
    "Energy Export Credit" and its own bonus credit. PG&E settles at the
    Permission To Operate anniversary and the CCA on its own cash-out year, so a
    single total is a figure no statement shows and that never settles as a
    whole.
    """
    profile = _cca_profile()
    bills = [
        _cycle_bill(profile, date(2026, 6, 1), date(2026, 6, 30), exported=2.0),
        _cycle_bill(profile, date(2026, 7, 1), date(2026, 7, 31), exported=2.0),
    ]
    state = bank.fold(profile, bills)

    assert state.split, "a CCA account is two banks"
    utility = state.held_by("utility")
    generation = state.held_by("generation")
    assert utility == pytest.approx(state.balance.delivery + state.balance.bonus)
    assert generation == pytest.approx(state.balance.generation + state.balance.cca_bonus)
    assert utility + generation == pytest.approx(state.balance.total)
    # Neither alone is the whole bank, which is the point.
    assert utility != pytest.approx(state.balance.total)


def test_a_bundled_account_holds_one_bank() -> None:
    """PG&E supplies generation too, so all three buckets are its own."""
    profile = _profile()
    bills = [_cycle_bill(profile, date(2026, 6, 1), date(2026, 6, 30), exported=2.0)]
    state = bank.fold(profile, bills)

    assert not state.split
    assert state.held_by("utility") == pytest.approx(state.balance.total)
    assert state.held_by("generation") == pytest.approx(state.balance.total)


def test_a_trailing_hole_is_reported_once_there_is_a_clock_to_see_it() -> None:
    """A missing hour at the *end* is not a gap between readings.

    Nothing in the series itself reveals it -- the day it belongs to still has
    its other twenty-three hours and prices without complaint -- so only a clock
    can tell an hour that arrived empty from one that has not arrived. That is
    what `through` gives :func:`tariffkit.billing.check_coverage`, and without it
    a meter that stopped reporting goes on quietly producing a smaller number
    that still calls itself complete.
    """
    from custom_components.tariffkit.energy import coverage_warnings

    period = BillingPeriod(date(2026, 7, 1), date(2026, 7, 2))
    full = [
        IntervalReading(datetime(2026, 7, day, hour, tzinfo=PACIFIC), imported=1.0)
        for day in (1, 2)
        for hour in range(24)
    ]
    closed = datetime(2026, 7, 3, 6, tzinfo=PACIFIC)
    assert coverage_warnings(full, period, closed) == ()

    # One missing hour is counted -- 1h of 48 is over the tolerance -- but the
    # series has not *stopped*, so it is not accused of having.
    brief = coverage_warnings(full[:-1], period, closed)
    assert any("1.0h missing" in warning for warning in brief)
    assert not any("the series stops at" in warning for warning in brief)

    # A meter silent for a day is named, and told where it fell over.
    stopped = coverage_warnings(full[:-24], period, closed)
    assert any("the series stops at" in warning for warning in stopped)
    # Named by where the data ends, not by the last reading's start.
    assert any("2026-07-02T00:00" in warning for warning in stopped)

    # And the missing hours really did change the priced total, which is what
    # made the silence expensive.
    profile = _profile()
    whole, _ = price(profile, full, period)
    short, _ = price(profile, full[:-24], period)
    assert whole is not None and short is not None
    assert short.imported_kwh < whole.imported_kwh


def test_a_period_still_running_is_not_told_its_remainder_is_missing() -> None:
    """The shortfall is measured against elapsed time, not the whole period."""
    from custom_components.tariffkit.energy import coverage_warnings

    period = BillingPeriod(date(2026, 7, 1), date(2026, 7, 31))
    so_far = [
        IntervalReading(datetime(2026, 7, day, hour, tzinfo=PACIFIC), imported=1.0)
        for day in range(1, 3)
        for hour in range(24)
    ]
    # Two days into a month-long cycle, with two days of readings.
    assert coverage_warnings(so_far, period, datetime(2026, 7, 3, 0, tzinfo=PACIFIC)) == ()


def _long_run(profile: AccountProfile, opens: date, closes: date) -> list:
    """Monthly cycles on the 15th, heavily exporting, long enough to cross a year."""
    bills, start = [], opens
    while start < closes:
        end = (start.replace(day=15) + timedelta(days=31)).replace(day=14)
        readings, day = [], start
        while day <= end:
            readings += [
                IntervalReading(
                    datetime(day.year, day.month, day.day, hour, tzinfo=PACIFIC),
                    imported=0.1,
                    exported=3.0,
                )
                for hour in range(24)
            ]
            day += timedelta(days=1)
        bill, _ = price(profile, readings, BillingPeriod(start, end))
        if bill is not None:
            bills.append(bill)
        start = end + timedelta(days=1)
    return bills


def _cca_long() -> AccountProfile:
    from tariffkit.config import CcaConfig
    from tariffkit.models import Supplier

    config = Config(
        tariff="E-ELEC",
        pto_date=date(2026, 6, 17),
        supplier=Supplier.CCA,
        baseline_territory="X",
        cca=CcaConfig(name="MCE", rate_card="MCE", option="light_green", pcia_vintage=2011),
    )
    return AccountProfile((AccountEpoch(date(2026, 1, 1), config),), name="probe")


def test_every_annual_settlement_is_applied_not_only_the_last() -> None:
    """`run_true_ups` computes each event from whatever ledger it is handed.

    So a run crossing two of them yields a second event derived from a ledger
    that never saw the first one's clawback. Taking the last event's closing
    balance therefore discards every earlier reversal -- and on a CCA account
    whose PTO anniversary falls after April, which is most of the year, the last
    event is the utility's, whose CCA branch reverses nothing at all. The whole
    cash-out then survives in the bank as credit already paid out in cash.
    """
    from tariffkit.billing import run_ledger, run_true_ups

    profile = _cca_long()
    bills = _long_run(profile, date(2026, 6, 15), date(2027, 8, 15))
    state = bank.fold(profile, bills)

    events = run_true_ups(run_ledger(bills).entries, pto_date=date(2026, 6, 17), is_cca=True)
    assert len(events) >= 2, "the run must cross both an MCE and a PG&E settlement"
    reversal = max(event.reversal for event in events)
    assert reversal > 0, "one of them must claw credit back"

    # Chained by hand, recomputing each event from a ledger carrying the last.
    opening, remaining = None, list(bills)
    while remaining:
        entries = run_ledger(remaining, opening=opening).entries
        found = run_true_ups(entries, pto_date=date(2026, 6, 17), is_cca=True)
        if not found:
            break
        opening = found[0].closing
        remaining = [b for b in remaining if b.period.start > found[0].period.end]
    correct = run_ledger(remaining, opening=opening).entries[-1].closing if remaining else opening
    assert correct is not None
    assert state.balance.total == pytest.approx(correct.total, abs=0.01)
    # And the defect this guards against is not a rounding error.
    naive = (
        run_ledger(
            [b for b in bills if b.period.start > events[-1].period.end],
            opening=events[-1].closing,
        )
        .entries[-1]
        .closing
    )
    # The discarded amount is the part of the reversal the bank could cover.
    # It used to equal the reversal exactly; now that the reversal is computed
    # at the rate MCE's tariff actually names -- "including Solar Bonus Credit"
    # -- it can exceed the balance, and the tariff says so: the reversal "will
    # be charged against any Export Credit Balance available, otherwise it will
    # be charged against the NSC payment".
    assert naive.total - correct.total == pytest.approx(reversal, abs=0.01)
    assert len(state.true_ups) == len(events)


def test_a_bundled_account_is_not_told_a_cca_settled_it() -> None:
    """`run_true_ups` emits a cash-out for every March-April year regardless.

    It is written for the common case of a CCA account. A bundled customer has
    no aggregator to settle with, so both the event and its clawback are
    inventions.
    """
    from tariffkit.models import Supplier

    config = Config(
        tariff="E-ELEC",
        pto_date=date(2026, 6, 17),
        supplier=Supplier.BUNDLED,
        baseline_territory="X",
    )
    profile = AccountProfile((AccountEpoch(date(2026, 1, 1), config),), name="probe")
    state = bank.fold(profile, _long_run(profile, date(2026, 6, 15), date(2027, 8, 15)))

    assert state.true_ups, "the PTO anniversary is still crossed"
    assert not any("mce" in label for label in state.true_ups)
    assert not state.split


def test_a_settlement_that_changes_nothing_does_not_consume_cycles() -> None:
    """On a CCA account the utility's true-up settles nothing under SC 5.a.

    It must therefore not shorten the *other* supplier's cash-out year. A
    cash-out reverses its window's surplus, so a year cut from twelve months to
    nine claws back less than the tariff requires and leaves the difference
    sitting in the bank.
    """
    from tariffkit.billing import run_ledger, run_true_ups

    profile = _cca_long()
    bills = _long_run(profile, date(2026, 6, 15), date(2029, 8, 15))
    state = bank.fold(profile, bills)

    # The library's own windows, taken over the whole run, are the natural ones.
    natural = [
        (event.period.start, event.period.end)
        for event in run_true_ups(
            run_ledger(bills).entries, pto_date=date(2026, 6, 17), is_cca=True
        )
        if str(event.kind) == "mce_cash_out"
    ]
    assert len(natural) >= 2
    for start, end in natural:
        # A full cash-out year, not one truncated by an unrelated anniversary.
        assert (end - start).days > 300, (start, end)

    # Every settlement is still recorded, including the ones that settle nothing.
    assert any("pge_relevant_period" in label for label in state.true_ups)
    assert sum("mce_cash_out" in label for label in state.true_ups) == len(natural)


def test_a_supplier_change_inside_the_run_is_refused_not_guessed() -> None:
    """An annual settlement settles a year, not a cycle.

    The library gives no way to say a year was half one arrangement and half
    another, so a run spanning a change is folded under one of them. Saying so
    is the only honest option available.
    """
    from tariffkit.config import CcaConfig
    from tariffkit.models import Supplier

    bundled = Config(tariff="E-ELEC", pto_date=PTO, baseline_territory="X")
    cca = Config(
        tariff="E-ELEC",
        pto_date=PTO,
        supplier=Supplier.CCA,
        baseline_territory="X",
        cca=CcaConfig(name="MCE", rate_card="MCE", option="light_green", pcia_vintage=2011),
    )
    profile = AccountProfile(
        (AccountEpoch(date(2026, 1, 1), bundled), AccountEpoch(date(2026, 8, 1), cca)),
        name="probe",
    )
    bills = [
        _cycle_bill(profile, date(2026, 6, 1), date(2026, 6, 30), exported=2.0),
        _cycle_bill(profile, date(2026, 9, 1), date(2026, 9, 30), exported=2.0),
    ]
    state = bank.fold(profile, bills)
    assert not state.trustworthy
    assert any("changed supplier" in w for w in state.warnings)


def test_the_bank_finds_a_pto_recorded_only_on_an_earlier_epoch() -> None:
    """A later epoch omitting it must not erase the annual settlement.

    Folding with no PTO applies no settlement at all, silently, and reports the
    result as complete.
    """
    early = Config(tariff="E-ELEC", pto_date=PTO, baseline_territory="X")
    later = Config(tariff="E-ELEC", vintage="NBT00", baseline_territory="X")
    profile = AccountProfile(
        (AccountEpoch(date(2026, 1, 1), early), AccountEpoch(date(2026, 9, 1), later)),
        name="probe",
    )
    assert profile.pto_date == PTO
