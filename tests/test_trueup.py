"""The annual true-up.

Nothing here is reconciled against a statement, because no true-up statement
exists yet: the first MCE cash-out falls after the March-April 2027 cycle and
the first PG&E Relevant Period ends on the 2027 PTO anniversary. What these
tests pin is the tariff text -- the two calendars, the surplus test, the credit
reversal, and the fact that credits carry forward rather than expiring.
"""

from __future__ import annotations

from datetime import date

import pytest

from nem_rates.billing import BillingPeriod
from nem_rates.billing.ledger import CreditBalances, CreditBucket, LedgerEntry
from nem_rates.billing.trueup import (
    CHECK_THRESHOLD,
    TrueUpKind,
    average_export_rate,
    cash_out_periods,
    mce_cash_out,
    pge_true_up,
    published_nsc_rate,
    relevant_period_end,
    run_true_ups,
)
from nem_rates.errors import ConfigError, DataError


def entry(
    start: date,
    end: date,
    *,
    imported: float = 0.0,
    exported: float = 0.0,
    earned_generation: float = 0.0,
    closing_generation: float = 0.0,
) -> LedgerEntry:
    balances = CreditBalances(generation=closing_generation)
    return LedgerEntry(
        period=BillingPeriod(start, end),
        opening=CreditBalances(),
        earned=CreditBalances(generation=earned_generation),
        applied=CreditBalances(),
        closing=balances,
        cash_due=0.0,
        gross_charges=0.0,
        non_offsettable=0.0,
        imported_kwh=imported,
        exported_kwh=exported,
    )


def year_of_cycles(**kw: float) -> list[LedgerEntry]:
    """Ten cycles ending with a March-April one, sharing the same numbers.

    Ten rather than twelve: the helper builds within-year month pairs, so the
    December-January cycle is elided. The count is incidental to what these
    tests check -- that a run closes on a March-April cycle -- but the
    arithmetic in the surplus tests is written against ten, so it is stated.
    """
    months = [(5, 6), (6, 7), (7, 8), (8, 9), (9, 10), (10, 11), (11, 12)]
    out = [entry(date(2026, a, 15), date(2026, b, 14), **kw) for a, b in months]  # type: ignore[arg-type]
    out += [
        entry(date(2027, a, 15), date(2027, b, 14), **kw)  # type: ignore[arg-type]
        for a, b in [(1, 2), (2, 3), (3, 4)]
    ]
    return out


class TestPublishedRate:
    def test_a_known_month(self) -> None:
        assert published_nsc_rate(date(2026, 8, 20)) == 0.02684

    def test_the_day_within_the_month_does_not_matter(self) -> None:
        assert published_nsc_rate(date(2026, 1, 1)) == published_nsc_rate(date(2026, 1, 31))

    def test_an_unvendored_month_says_what_to_do(self) -> None:
        with pytest.raises(DataError) as caught:
            published_nsc_rate(date(2030, 1, 1))
        message = str(caught.value)
        assert "2030-01" in message and "nsc_rate" in message


class TestRelevantPeriod:
    def test_the_next_anniversary_of_the_pto_date(self) -> None:
        assert relevant_period_end(date(2026, 6, 3), date(2026, 6, 30)) == date(2027, 6, 3)

    def test_an_anniversary_later_this_year_counts(self) -> None:
        assert relevant_period_end(date(2026, 6, 3), date(2027, 1, 1)) == date(2027, 6, 3)

    def test_the_anniversary_itself_is_not_after_itself(self) -> None:
        assert relevant_period_end(date(2026, 6, 3), date(2027, 6, 3)) == date(2028, 6, 3)

    def test_the_pto_date_itself_does_not_close_the_first_period(self) -> None:
        # The PTO date starts the first Relevant Period; the first close is a
        # year later, even for a ledger that begins before interconnection.
        assert relevant_period_end(date(2026, 6, 3), date(2026, 5, 15)) == date(2027, 6, 3)

    def test_a_leap_day_pto_falls_back_to_the_28th(self) -> None:
        assert relevant_period_end(date(2024, 2, 29), date(2026, 1, 1)) == date(2026, 2, 28)


class TestCashOutPeriods:
    def test_a_year_closes_on_the_march_april_cycle(self) -> None:
        groups = cash_out_periods(year_of_cycles())
        assert len(groups) == 1
        assert groups[0][-1].period.start.month == 3

    def test_an_unclosed_run_is_one_open_group(self) -> None:
        cycles = [entry(date(2026, m, 15), date(2026, m + 1, 14)) for m in (5, 6, 7)]
        groups = cash_out_periods(cycles)
        assert len(groups) == 1
        assert groups[0][-1].period.start.month == 7

    def test_cycles_after_a_close_start_a_new_group(self) -> None:
        cycles = [*year_of_cycles(), entry(date(2027, 4, 15), date(2027, 5, 14))]
        groups = cash_out_periods(cycles)
        assert len(groups) == 2
        assert len(groups[1]) == 1


class TestMceCashOut:
    def test_importing_more_than_exporting_pays_nothing(self) -> None:
        got = mce_cash_out(year_of_cycles(imported=100.0, exported=40.0))
        assert not got.eligible
        assert got.surplus_kwh == 0.0
        assert got.cash_out == 0.0

    def test_the_balance_rolls_forward_rather_than_expiring(self) -> None:
        # The widely repeated "credits reset to zero at true-up" is NEM 2.0. The
        # SBP tariff says the balance rolls over "indefinitely".
        got = mce_cash_out(year_of_cycles(imported=100.0, exported=40.0, closing_generation=12.0))
        assert got.closing.generation == pytest.approx(12.0)

    def test_surplus_is_paid_at_the_configured_rate_net_of_the_reversal(self) -> None:
        # 10 cycles x (10 imported, 30 exported) = 200 kWh surplus. Export credit
        # earned is 10 x $3 = $30 over 300 kWh exported, so the average rate is
        # $0.10/kWh and the reversal is 200 x 0.10 = $20.
        cycles = year_of_cycles(
            imported=10.0, exported=30.0, earned_generation=3.0, closing_generation=50.0
        )
        got = mce_cash_out(cycles, nsc_rate=0.15)
        assert got.eligible
        assert got.surplus_kwh == pytest.approx(200.0)
        assert got.reversal == pytest.approx(20.0)
        assert got.nsc_payment == pytest.approx(30.0)
        # The balance absorbed the whole reversal, so the payment is untouched.
        assert got.closing.generation == pytest.approx(30.0)
        assert got.cash_out == pytest.approx(30.0)
        assert not got.estimated

    def test_a_reversal_the_balance_cannot_absorb_comes_off_the_payment(self) -> None:
        cycles = year_of_cycles(
            imported=10.0, exported=30.0, earned_generation=3.0, closing_generation=5.0
        )
        got = mce_cash_out(cycles, nsc_rate=0.15)
        # $20 reversal, only $5 of balance to absorb it; $15 hits the payment.
        assert got.closing.generation == pytest.approx(0.0)
        assert got.cash_out == pytest.approx(30.0 - 15.0)

    def test_a_reversal_larger_than_the_payment_floors_at_zero(self) -> None:
        cycles = year_of_cycles(imported=10.0, exported=30.0, earned_generation=3.0)
        got = mce_cash_out(cycles, nsc_rate=0.01)
        assert got.cash_out == 0.0
        assert any("floored" in n for n in got.notes)

    def test_no_configured_rate_falls_back_and_says_so(self) -> None:
        cycles = year_of_cycles(imported=10.0, exported=30.0, earned_generation=3.0)
        got = mce_cash_out(cycles)
        assert got.estimated
        assert got.nsc_rate is not None
        assert any("stand-in" in n for n in got.notes)

    def test_a_large_cash_out_is_paid_by_check(self) -> None:
        cycles = year_of_cycles(imported=0.0, exported=1000.0)
        got = mce_cash_out(cycles, nsc_rate=0.05)
        assert got.cash_out > CHECK_THRESHOLD
        assert got.paid_by_check

    def test_nothing_is_ever_reported_as_verified(self) -> None:
        # No true-up statement has been reconciled; the flag must not drift.
        assert not mce_cash_out(year_of_cycles()).verified

    def test_an_empty_period_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            mce_cash_out([])


class TestPgeTrueUp:
    def test_a_cca_account_is_paid_nothing_even_with_surplus(self) -> None:
        # Schedule NBT, Special Condition 5.a. This is the finding that makes
        # PG&E's published rate the wrong input for this account.
        cycles = year_of_cycles(imported=10.0, exported=30.0, earned_generation=3.0)
        got = pge_true_up(cycles, date(2026, 6, 3), is_cca=True)
        assert got.surplus_kwh > 0
        assert not got.eligible
        assert got.cash_out == 0.0
        assert got.nsc_rate is None
        assert any("Special Condition" in n for n in got.notes)

    def test_a_cca_balance_carries_forward_untouched(self) -> None:
        cycles = year_of_cycles(imported=10.0, exported=30.0, closing_generation=40.0)
        got = pge_true_up(cycles, date(2026, 6, 3), is_cca=True)
        assert got.closing.generation == pytest.approx(40.0)

    def test_a_bundled_account_with_surplus_is_paid(self) -> None:
        cycles = year_of_cycles(
            imported=10.0, exported=30.0, earned_generation=3.0, closing_generation=50.0
        )
        got = pge_true_up(cycles, date(2026, 6, 3), is_cca=False)
        assert got.eligible
        assert got.nsc_rate is not None
        assert got.cash_out > 0

    def test_a_bundled_account_without_surplus_is_not(self) -> None:
        cycles = year_of_cycles(imported=100.0, exported=40.0)
        got = pge_true_up(cycles, date(2026, 6, 3), is_cca=False)
        assert not got.eligible
        assert got.cash_out == 0.0

    def test_the_period_notes_the_anniversary_it_was_measured_against(self) -> None:
        got = pge_true_up(year_of_cycles(), date(2026, 6, 3), is_cca=True)
        assert "2027-06-03" in got.notes[0]


class TestAverageExportRate:
    def test_dollars_earned_over_kwh_exported(self) -> None:
        cycles = [
            entry(date(2026, 5, 15), date(2026, 6, 14), exported=100.0, earned_generation=8.0)
        ]
        assert average_export_rate(cycles, CreditBucket.GENERATION) == pytest.approx(0.08)

    def test_no_exports_is_zero_rather_than_a_division_error(self) -> None:
        cycles = [entry(date(2026, 5, 15), date(2026, 6, 14))]
        assert average_export_rate(cycles, CreditBucket.GENERATION) == 0.0


class TestRunTrueUps:
    def test_the_two_calendars_produce_two_separate_events(self) -> None:
        # The whole point of stage two: an April cash-out and a June true-up,
        # neither closing the other's bank.
        cycles = year_of_cycles(imported=10.0, exported=30.0, earned_generation=3.0)
        cycles += [entry(date(2027, m, 15), date(2027, m + 1, 14)) for m in (4, 5, 6, 7)]
        got = run_true_ups(cycles, pto_date=date(2026, 6, 3), is_cca=True, nsc_rate=0.05)
        kinds = [t.kind for t in got]
        assert TrueUpKind.MCE_CASH_OUT in kinds
        assert TrueUpKind.PGE_RELEVANT_PERIOD in kinds

    def test_the_period_includes_the_cycle_holding_the_anniversary(self) -> None:
        # The anniversary falls mid-cycle and the true-up lands on that cycle's
        # statement, so it closes the period rather than opening the next one.
        # Ending at the cycle before drops a month of energy out of the period.
        cycles = [
            entry(date(2026, 6, 30), date(2026, 7, 28), imported=10.0),
            *[entry(date(2026, m, 28), date(2026, m + 1, 27), imported=10.0) for m in range(7, 12)],
            *[entry(date(2027, m, 28), date(2027, m + 1, 27), imported=10.0) for m in range(1, 7)],
        ]
        got = run_true_ups(cycles, pto_date=date(2026, 6, 3), is_cca=True)
        period = next(t for t in got if t.kind is TrueUpKind.PGE_RELEVANT_PERIOD)
        assert period.period.start <= date(2027, 6, 3) <= period.period.end
        # Eleven of the twelve cycles: the last one opens the next period.
        assert period.period.end == date(2027, 6, 27)
        assert period.imported_kwh == pytest.approx(110.0)

    def test_an_unclosed_year_is_not_reported(self) -> None:
        # A year that has not closed has not been trued up.
        cycles = [entry(date(2026, m, 15), date(2026, m + 1, 14)) for m in (5, 6, 7)]
        assert run_true_ups(cycles, pto_date=date(2026, 6, 3)) == []

    def test_no_pto_date_yields_only_the_cca_side(self) -> None:
        cycles = year_of_cycles()
        got = run_true_ups(cycles)
        assert [t.kind for t in got] == [TrueUpKind.MCE_CASH_OUT]

    def test_no_cycles_at_all(self) -> None:
        assert run_true_ups([]) == []

    def test_results_come_back_in_date_order(self) -> None:
        cycles = year_of_cycles(imported=10.0, exported=30.0)
        cycles += [entry(date(2027, m, 15), date(2027, m + 1, 14)) for m in (4, 5, 6, 7)]
        got = run_true_ups(cycles, pto_date=date(2026, 6, 3), nsc_rate=0.05)
        assert [t.period.end for t in got] == sorted(t.period.end for t in got)


def test_to_dict_is_json_shaped() -> None:
    import json

    got = mce_cash_out(year_of_cycles(imported=10.0, exported=30.0), nsc_rate=0.05).to_dict()
    assert json.loads(json.dumps(got))["kind"] == "mce_cash_out"
    assert got["verified"] is False
