"""Export credit balances carried between cycles.

Reconciled against the two credit banks printed on the 2026-08-04 statement.
PG&E's spends everything it earns; MCE's earns more than it can spend, so the
cap binds and the excess carries -- between them they exercise both directions.
"""

from __future__ import annotations

from datetime import date

import pytest

from tariffkit.billing import (
    Bill,
    BillingPeriod,
    CreditBalances,
    CreditBucket,
    apply_credits,
    credits_earned,
    run_ledger,
)
from tariffkit.billing.ledger import CHARGE_OFFSETS, charges_by_bucket

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

    Bank prints: opening $4.93, earned $11.33, applied $3.63, remaining $12.63,
    and beside it the two balances that total: "Current Energy Export Credit
    (EEC) Balance $9.34", "Current Energy Export Bonus Credit (EEBC) Balance
    $3.29". Charges are 4.84 generation less the 0.25 cost relief credit and the
    0.96 solar bonus, giving the $3.63 "Net Charges" the statement shows.

    The bonus here is ``cca_acc_plus``, not ``acc_plus``: PG&E credits its own
    adder of $1.71 on its page and spends it there in the same cycle, so the
    $1.70 on this page is a second credit rather than the same one seen twice.
    """
    return Bill(
        period=PERIOD,
        import_components={"cca_generation": 4.84, "cca_cost_relief_credit": -0.25},
        export_components={
            "cca_generation": -9.63,
            "cca_solar_bonus": -0.96,
            "cca_acc_plus": -1.70,
        },
    )


def _payout_bill() -> Bill:
    """A cycle whose credits outweigh its charges.

    `baseline_credit` is a negative import component and counts as
    non-offsettable, so at a high enough export ratio both `non_offsettable` and
    `gross_charges` go negative while `cash_due` stays at zero -- a statement
    charges nothing rather than paying out. Shares its shape with
    ``TestAStatementNeverPaysYou``, which is where that rule is pinned.
    """
    return Bill(
        period=PERIOD,
        import_components={"distribution": 2.0, "baseline_credit": -12.0},
        export_components={"delivery": -40.0},
    )


#: The statement's own split of MCE's $4.93 opening: EEC $3.34, EEBC $1.59 --
#: June's adder, which nothing has spent.
MCE_OPENING = CreditBalances(generation=3.34, cca_bonus=1.59)


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

    def test_non_bypassable_charges_are_reachable_only_by_the_bonus(self) -> None:
        """Schedule NBT SC 2.f: "except for the ACC Plus credit".

        The four NBCs -- PPP, wildfire fund, CTC, nuclear decommissioning --
        plus the PCIA and franchise fee are out of reach of a scoped export
        credit, and within reach of the bonus adder. SC 2.d: "export credits
        associated with the ACC plus adder may be used to offset any charges
        incurred by the customer."
        """
        offsettable, non_offsettable, _ = charges_by_bucket(pge_bill())
        nbcs_and_more = 0.24 + 0.23 + 0.01 + 1.39 + 0.02
        # They used to sit outside every bucket, payable in cash.
        assert non_offsettable == pytest.approx(0.0)
        assert offsettable[CreditBucket.BONUS] >= nbcs_and_more
        # And a generation-scoped credit still cannot reach them.
        assert offsettable[CreditBucket.GENERATION] == pytest.approx(0.0)

    def test_the_fixed_charge_is_reachable_by_the_bonus_credit(self) -> None:
        # It was modelled as out of reach until a statement said otherwise. The
        # 2026-07-07 bill applies $1.59 of bonus credit where the energy charges
        # alone leave room for $0.92, and PG&E's wording is that the bonus
        # offsets anything not explicitly non-bypassable. A daily charge for
        # grid access is not that.
        offsettable, non_offsettable, _ = charges_by_bucket(pge_bill())
        assert offsettable[CreditBucket.BONUS] >= 23.01
        assert non_offsettable < 23.01


class TestMceBank:
    """Credits exceed charges here, so the cap binds and the excess carries."""

    def test_reconciles_against_the_printed_bank(self) -> None:
        entry = apply_credits(mce_bill(), MCE_OPENING)
        assert entry.opening.total == pytest.approx(4.93)
        assert entry.earned.total == pytest.approx(11.33)
        assert entry.applied.total == pytest.approx(3.63)
        assert entry.closing.total == pytest.approx(12.63)
        assert entry.cash_due == pytest.approx(0.0)

    def test_the_two_printed_balances_are_tracked_apart(self) -> None:
        """The statement prints EEC and EEBC as separate balances, so do we.

        Totalling them was enough to reconcile this cycle and not enough to
        stay right: the EEBC is never applied -- MCE's "Energy Export Bonus
        Credits Applied" line prints $0.00 here -- so a model that lets it
        offset generation charges alongside the EEC drains a balance the
        statement shows growing every cycle.
        """
        entry = apply_credits(mce_bill(), MCE_OPENING)
        assert entry.closing.generation == pytest.approx(9.34)
        assert entry.closing.cca_bonus == pytest.approx(3.29)
        assert entry.applied.cca_bonus == pytest.approx(0.0)

    def test_the_cca_bonus_belongs_to_the_cca_bank(self) -> None:
        """Not the utility's, whose own bonus column closes at $0.00."""
        closing = apply_credits(mce_bill(), MCE_OPENING).closing
        assert closing.held_by("generation", split=True) == pytest.approx(12.63)
        assert closing.held_by("utility", split=True) == pytest.approx(0.0)

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


class TestThePublishedTermsReconcile:
    """What a consumer adds up has to reach the state.

    `amount_due_*` publishes a breakdown, and the in-cycle charge offset used to
    appear in none of it: not in `export_credits`, not in `credit_applied`. A
    consumer summing the terms landed short by exactly that, with no way to tell
    a missing term from a rounding error. `gross_charges` is the term that
    closes it, so the identity it rests on is pinned here.
    """

    def test_charge_components_alone_do_not_reach_the_state(self) -> None:
        """The gap is the offset, not an error -- which is why it needed a name."""
        bill = mce_bill()
        entry = apply_credits(bill, MCE_OPENING)

        naive = bill.energy_charges + bill.taxes + bill.fixed_charges - entry.applied.total
        offset = sum(
            abs(value) for name, value in bill.export_components.items() if name in CHARGE_OFFSETS
        )
        assert offset > 0, "the fixture has to exercise an offset for this to mean anything"
        assert naive - entry.cash_due == pytest.approx(offset)

    #: Every shape the published terms have to reconcile over, the last of them
    #: the one where the zero floor binds and an unfloored identity breaks.
    CASES = (
        ("cca in-cycle offset", mce_bill, MCE_OPENING),
        ("plain cycle", pge_bill, None),
        ("bank larger than the charges", pge_bill, CreditBalances(bonus=10_000.0)),
        ("credit outweighs the charges", _payout_bill, None),
    )

    def test_the_floor_is_what_makes_it_exact(self) -> None:
        """`gross - applied` alone is short wherever a statement declines to pay."""
        entry = apply_credits(_payout_bill())
        assert entry.gross_charges < 0, "this case has to reach the floor to mean anything"
        assert entry.gross_charges - entry.applied.total != pytest.approx(entry.cash_due)
        assert entry.cash_due == pytest.approx(0.0)

    def test_the_published_terms_reach_the_state(self) -> None:
        for label, make, opening in self.CASES:
            entry = apply_credits(make(), opening)
            not_paid_out = max(0.0, entry.applied.total - entry.gross_charges)
            reached = entry.gross_charges - entry.applied.total + not_paid_out
            assert reached == pytest.approx(entry.cash_due), label

    def test_a_large_enough_bonus_bank_reaches_every_charge(self) -> None:
        """Schedule NBT sheet 19: "can offset all charges including the NBCs".

        This used to assert the opposite -- that a bonus bank of any size left
        the non-bypassable charges standing as cash owed. The tariff says three
        separate times that the ACC Plus credit is the one exception to their
        non-bypassability.
        """
        entry = apply_credits(pge_bill(), CreditBalances(bonus=10_000.0))
        assert entry.cash_due == pytest.approx(0.0)

        payout = apply_credits(_payout_bill())
        assert payout.cash_due == pytest.approx(max(0.0, payout.non_offsettable))


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
        offsettable, _, _ = charges_by_bucket(self.bill())
        # The non-bypassable charges sit in the bonus bucket now, out of reach
        # of a generation-scoped offset, which is the property under test. An
        # overrun banks instead of reaching them.
        assert offsettable[CreditBucket.GENERATION] == pytest.approx(0.0)
        assert offsettable[CreditBucket.BONUS] >= 0.50

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


class TestABankIsHeldByWhoeverSettlesIt:
    """Which buckets belong to which party, answered in one place.

    The Home Assistant integration used to hold its own copy of this map, and
    the copy disagreed with :mod:`tariffkit.billing.trueup` about who owns the
    generation bucket.
    """

    BANK = CreditBalances(generation=100.0, delivery=30.0, bonus=12.0)

    def test_a_cca_account_has_two_banks(self) -> None:
        assert self.BANK.held_by("utility", split=True) == pytest.approx(42.0)
        assert self.BANK.held_by("generation", split=True) == pytest.approx(100.0)

    def test_the_two_halves_are_the_whole(self) -> None:
        halves = self.BANK.held_by("utility", split=True) + self.BANK.held_by(
            "generation", split=True
        )
        assert halves == pytest.approx(self.BANK.total)

    def test_a_bundled_account_has_one(self) -> None:
        """Both names return the whole bank, so neither reads as a fraction."""
        assert self.BANK.held_by("utility", split=False) == pytest.approx(142.0)
        assert self.BANK.held_by("generation", split=False) == pytest.approx(142.0)

    def test_an_unknown_party_raises_rather_than_guessing(self) -> None:
        with pytest.raises(ValueError, match="unknown party"):
            self.BANK.held_by("someone_else", split=True)


class TestAStatementNeverPaysYou:
    """``cash_due`` is floored at zero, and the shortfall stays in the bank.

    ``non_offsettable`` can go negative on its own -- ``baseline_credit`` is a
    negative import component listed there -- and at a high enough export ratio
    it outweighs the charges beside it. Reporting a negative amount owed would
    contradict the one thing ``apply_credits`` exists to get right.
    """

    def _bill(self) -> Bill:
        return Bill(
            period=PERIOD,
            import_components={"distribution": 2.0, "baseline_credit": -12.0},
            export_components={"delivery": -40.0},
        )

    def test_the_amount_due_is_not_negative(self) -> None:
        entry = apply_credits(self._bill())
        assert self._bill().total < 0
        assert entry.cash_due == pytest.approx(0.0)

    def test_the_credit_that_could_not_be_spent_is_still_banked(self) -> None:
        entry = apply_credits(self._bill())
        assert entry.closing.total == pytest.approx(entry.earned.total - entry.applied.total)
        assert entry.applied.total <= entry.earned.total

    def test_nothing_is_lost_between_earned_applied_and_closing(self) -> None:
        entry = apply_credits(self._bill(), CreditBalances(delivery=5.0))
        assert entry.opening.total + entry.earned.total == pytest.approx(
            entry.applied.total + entry.closing.total
        )


class TestInCycleOffsetsSurviveTheCycle:
    """The true-up reverses at a rate that includes them, so they must."""

    def _bill(self) -> Bill:
        return Bill(
            period=BillingPeriod(start=date(2026, 7, 1), end=date(2026, 7, 31)),
            import_components={"generation": 20.0},
            export_components={"cca_generation": -5.0, "cca_solar_bonus": -0.5},
        )

    def test_the_solar_bonus_is_absent_from_earned(self) -> None:
        """It is spent against charges, not banked -- that is what it is."""
        entry = apply_credits(self._bill())
        assert entry.earned.generation == pytest.approx(5.0)

    def test_but_it_is_recorded_as_an_in_cycle_offset(self) -> None:
        """Without this the reversal averaged 5.00 on a cycle that earned 5.50."""
        entry = apply_credits(self._bill())
        assert entry.in_cycle_offsets.generation == pytest.approx(0.5)
