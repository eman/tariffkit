"""Export credit balances carried between cycles.

Reconciled against the two credit banks printed on the 2026-08-04 statement.
PG&E's spends everything it earns; MCE's earns more than it can spend, so the
cap binds and the excess carries -- between them they exercise both directions.
"""

from __future__ import annotations

from datetime import date

import pytest

from nem_rates.billing import (
    Bill,
    BillingPeriod,
    CreditBalances,
    CreditBucket,
    apply_credits,
    credits_earned,
    run_ledger,
)
from nem_rates.billing.ledger import charges_by_bucket

PERIOD = BillingPeriod(date(2026, 6, 30), date(2026, 7, 28))


def pge_bill() -> Bill:
    """The PG&E half of the statement.

    Bank prints: opening $0.00, earned $7.96, applied $7.96, remaining $0.00,
    split into Energy Delivered Credits $6.25 and Bonus Credits $1.71.
    """
    return Bill(
        period=PERIOD,
        import_components={
            "distribution": 6.19,
            "transmission": 1.81,
            "pcia": 1.39,
            "public_purpose_programs": 0.24,
            "wildfire_fund_charge": 0.23,
            "wildfire_hardening": 0.15,
            "franchise_fee_surcharge": 0.02,
            "competition_transition_charges": 0.01,
        },
        export_components={"delivery": -6.25, "acc_plus": -1.71},
        fixed_components={"base_services_charge": 23.01},
    )


def mce_bill() -> Bill:
    """The MCE half.

    Bank prints: opening $4.93, earned $11.33, applied $3.63, remaining $12.63.
    Charges are 4.84 generation less the 0.25 cost relief credit and the 0.96
    solar bonus, giving the $3.63 "Net Charges" the statement shows.
    """
    return Bill(
        period=PERIOD,
        import_components={"cca_generation": 4.84, "cca_cost_relief_credit": -0.25},
        export_components={"cca_generation": -9.63, "cca_solar_bonus": -0.96, "acc_plus": -1.70},
    )


class TestPgeBank:
    def test_credits_bank_into_the_columns_the_statement_prints(self) -> None:
        earned = credits_earned(pge_bill())
        assert earned.delivery == pytest.approx(6.25)
        assert earned.bonus == pytest.approx(1.71)
        assert earned.generation == 0.0
        assert earned.total == pytest.approx(7.96)

    def test_everything_earned_is_spent(self) -> None:
        entry = apply_credits(pge_bill())
        assert entry.applied.total == pytest.approx(7.96)
        assert entry.closing.total == pytest.approx(0.0)

    def test_delivery_credit_is_spent_on_delivery_charges(self) -> None:
        entry = apply_credits(pge_bill())
        assert entry.applied.delivery == pytest.approx(6.25)

    def test_non_bypassable_charges_are_not_offsettable(self) -> None:
        """Not even by the bonus credit -- that is what non-bypassable means."""
        _, non_offsettable, _ = charges_by_bucket(pge_bill())
        # PPP + wildfire fund + CTC, plus PCIA, franchise fee and the fixed charge.
        assert non_offsettable == pytest.approx(0.24 + 0.23 + 0.01 + 1.39 + 0.02 + 23.01)


class TestMceBank:
    """Credits exceed charges here, so the cap binds and the excess carries."""

    def test_reconciles_against_the_printed_bank(self) -> None:
        entry = apply_credits(mce_bill(), CreditBalances(generation=4.93))
        assert entry.opening.total == pytest.approx(4.93)
        assert entry.earned.total == pytest.approx(11.33)
        assert entry.applied.total == pytest.approx(3.63)
        assert entry.closing.total == pytest.approx(12.63)
        assert entry.cash_due == pytest.approx(0.0)

    def test_solar_bonus_reduces_charges_instead_of_banking(self) -> None:
        """The regression this class exists for.

        The library computes the 10% solar bonus as part of the export credit,
        but the statement prints it on the charges side, between the cost relief
        credit and "Net Charges". Banking it overstates both credits earned and
        credits applied by exactly its own $0.96, while still landing on the
        right closing balance because it is spent the same cycle -- so only the
        earned and applied figures catch it.
        """
        assert credits_earned(mce_bill()).total == pytest.approx(11.33)
        offsettable, _, _ = charges_by_bucket(mce_bill())
        assert offsettable[CreditBucket.GENERATION] == pytest.approx(3.63)

    def test_unspent_credit_is_available_next_cycle(self) -> None:
        first = apply_credits(mce_bill(), CreditBalances(generation=4.93))
        # A quiet month: charges but no export at all.
        quiet = Bill(period=PERIOD, import_components={"cca_generation": 5.00})
        second = apply_credits(quiet, first.closing)
        assert second.earned.total == 0.0
        assert second.applied.generation == pytest.approx(5.00)
        assert second.cash_due == pytest.approx(0.0)
        assert second.closing.total == pytest.approx(12.63 - 5.00)


class TestInCycleOffsetOverrun:
    """An in-cycle offset larger than the charges it was meant to cover."""

    def bill(self) -> Bill:
        # Solar bonus 0.96 against only 0.20 of generation charge.
        return Bill(
            period=PERIOD,
            import_components={"cca_generation": 0.20, "public_purpose_programs": 0.50},
            export_components={"cca_solar_bonus": -0.96},
        )

    def test_it_cannot_reduce_non_bypassable_charges(self) -> None:
        """The excess must not leak into charges nothing is allowed to reduce.

        A generation-scoped offset reaching the non-bypassable charges would be
        exactly backwards -- non-bypassable is what those charges are.
        """
        _, non_offsettable, _ = charges_by_bucket(self.bill())
        assert non_offsettable == pytest.approx(0.50)

    def test_the_excess_banks_rather_than_becoming_cash_owed(self) -> None:
        """The statement's rule for any credit it cannot spend: saved for later."""
        entry = apply_credits(self.bill())
        assert entry.earned.generation == pytest.approx(0.76)  # 0.96 less the 0.20 it covered
        assert entry.closing.generation == pytest.approx(0.76)

    def test_cash_due_is_the_non_bypassable_charge(self) -> None:
        entry = apply_credits(self.bill())
        assert entry.cash_due == pytest.approx(0.50)
        assert entry.cash_due > 0


class TestScoping:
    def test_a_scoped_credit_cannot_reach_another_bucket(self) -> None:
        """Delivery credit against generation-only charges stays banked."""
        bill = Bill(
            period=PERIOD,
            import_components={"cca_generation": 10.0},
            export_components={"delivery": -4.0},
        )
        entry = apply_credits(bill)
        assert entry.applied.total == 0.0
        assert entry.closing.delivery == pytest.approx(4.0)
        assert entry.cash_due == pytest.approx(10.0)

    def test_bonus_reaches_what_a_scoped_credit_cannot(self) -> None:
        bill = Bill(
            period=PERIOD,
            import_components={"cca_generation": 10.0},
            export_components={"acc_plus": -4.0},
        )
        entry = apply_credits(bill)
        assert entry.applied.bonus == pytest.approx(4.0)
        assert entry.cash_due == pytest.approx(6.0)

    def test_scoped_credit_is_spent_before_the_flexible_one(self) -> None:
        """Otherwise the bonus is burnt on charges a scoped credit could cover,
        stranding the scoped credit against nothing it is allowed to offset."""
        bill = Bill(
            period=PERIOD,
            import_components={"distribution": 5.0},
            export_components={"delivery": -5.0, "acc_plus": -5.0},
        )
        entry = apply_credits(bill)
        assert entry.applied.delivery == pytest.approx(5.0)
        assert entry.applied.bonus == 0.0
        assert entry.closing.bonus == pytest.approx(5.0)

    def test_unknown_component_defaults_to_non_offsettable(self) -> None:
        """A new charge should need cash until someone says otherwise."""
        bill = Bill(
            period=PERIOD,
            import_components={"some_new_rider": 3.0},
            export_components={"acc_plus": -9.0},
        )
        entry = apply_credits(bill)
        assert entry.non_offsettable == pytest.approx(3.0)
        assert entry.cash_due == pytest.approx(3.0)
        assert entry.closing.bonus == pytest.approx(9.0)


class TestRunLedger:
    def test_balance_carries_across_cycles(self) -> None:
        exporting = Bill(
            period=BillingPeriod(date(2026, 6, 30), date(2026, 7, 28)),
            import_components={"cca_generation": 1.0},
            export_components={"cca_generation": -10.0},
        )
        importing = Bill(
            period=BillingPeriod(date(2026, 7, 29), date(2026, 8, 27)),
            import_components={"cca_generation": 6.0},
        )
        ledger = run_ledger([exporting, importing])
        assert [round(e.closing.total, 2) for e in ledger.entries] == [9.0, 3.0]
        assert ledger.closing.total == pytest.approx(3.0)
        assert ledger.cash_due == pytest.approx(0.0)

    def test_bills_are_ordered_by_period(self) -> None:
        """A ledger fed out of order must not spend credit before it is earned."""
        early = Bill(
            period=BillingPeriod(date(2026, 6, 30), date(2026, 7, 28)),
            export_components={"cca_generation": -10.0},
        )
        late = Bill(
            period=BillingPeriod(date(2026, 7, 29), date(2026, 8, 27)),
            import_components={"cca_generation": 4.0},
        )
        ledger = run_ledger([late, early])
        assert [e.period.start for e in ledger.entries] == [early.period.start, late.period.start]
        assert ledger.cash_due == pytest.approx(0.0)

    def test_opening_balance_lets_a_run_start_mid_year(self) -> None:
        bill = Bill(period=PERIOD, import_components={"cca_generation": 4.0})
        ledger = run_ledger([bill], CreditBalances(generation=10.0))
        assert ledger.closing.generation == pytest.approx(6.0)

    def test_empty_run(self) -> None:
        ledger = run_ledger([])
        assert ledger.entries == ()
        assert ledger.closing.total == 0.0
        assert ledger.cash_due == 0.0


class TestBalances:
    def test_negative_balance_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            CreditBalances(generation=-1.0)

    def test_serialises(self) -> None:
        entry = apply_credits(pge_bill())
        payload = entry.to_dict()
        assert payload["earned"]["total"] == pytest.approx(7.96)
        assert payload["period"]["days"] == 29
        # The charge scoping is not fully reconciled yet; say so rather than imply
        # a bill computed from it is exact.
        assert payload["complete"] is False
