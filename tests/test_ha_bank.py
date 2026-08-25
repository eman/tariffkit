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
    state = bank.fold(profile, bills, date(2026, 7, 31))

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
    state = bank.fold(profile, bills, date(2026, 8, 31))

    assert not state.trustworthy
    assert any("cannot be carried across a gap" in w for w in state.warnings)
    assert any("2026-06-30" in w and "2026-08-01" in w for w in state.warnings)


def test_no_cycles_means_no_bank_rather_than_zero() -> None:
    """Zero is a balance. Nothing priced is not a balance."""
    state = bank.fold(_profile(), [], date(2026, 7, 31))
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
    state = bank.fold(profile, [first], date(2026, 6, 30))
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
    state = bank.fold(profile, result.bills, date(2026, 7, 31))
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
    "Energy Export Credit". PG&E settles at the Permission To Operate
    anniversary and the CCA on its own cash-out year, so a single total is a
    figure no statement shows and that never settles as a whole.
    """
    profile = _cca_profile()
    bills = [
        _cycle_bill(profile, date(2026, 6, 1), date(2026, 6, 30), exported=2.0),
        _cycle_bill(profile, date(2026, 7, 1), date(2026, 7, 31), exported=2.0),
    ]
    state = bank.fold(profile, bills, date(2026, 7, 31))

    assert state.split, "a CCA account is two banks"
    utility = state.held_by("utility")
    generation = state.held_by("generation")
    assert utility == pytest.approx(state.balance.delivery + state.balance.bonus)
    assert generation == pytest.approx(state.balance.generation)
    assert utility + generation == pytest.approx(state.balance.total)
    # Neither alone is the whole bank, which is the point.
    assert utility != pytest.approx(state.balance.total)


def test_a_bundled_account_holds_one_bank() -> None:
    """PG&E supplies generation too, so all three buckets are its own."""
    profile = _profile()
    bills = [_cycle_bill(profile, date(2026, 6, 1), date(2026, 6, 30), exported=2.0)]
    state = bank.fold(profile, bills, date(2026, 6, 30))

    assert not state.split
    assert state.held_by("utility") == pytest.approx(state.balance.total)
    assert state.held_by("generation") == pytest.approx(state.balance.total)


def test_a_trailing_hole_is_invisible_to_the_coverage_check() -> None:
    """Which is why the bank has to ask the reader, not only the readings.

    A missing hour at the *end* of a window is not a gap between readings, so
    nothing in the series itself reveals it -- and the day it belongs to still
    has its other twenty-three hours and prices without complaint. That is the
    shape of every cycle rollover, because the recorder compiles a cycle's final
    hour after midnight has already opened the next one.
    """
    from custom_components.tariffkit.energy import coverage_warnings

    period = BillingPeriod(date(2026, 7, 1), date(2026, 7, 2))
    full = [
        IntervalReading(datetime(2026, 7, day, hour, tzinfo=PACIFIC), imported=1.0)
        for day in (1, 2)
        for hour in range(24)
    ]
    assert coverage_warnings(full, period) == ()
    assert coverage_warnings(full[:-1], period) == (), "a trailing hole is silent"

    # So the missing hour changes the priced total with nothing to flag it.
    profile = _profile()
    whole, _ = price(profile, full, period)
    short, _ = price(profile, full[:-1], period)
    assert whole is not None and short is not None
    assert short.imported_kwh < whole.imported_kwh
