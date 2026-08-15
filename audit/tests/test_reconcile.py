"""The map's invariants, and the comparison built on them.

``reconcile`` is pure, so everything here runs on constructed bills. The map
invariants are tested rather than trusted because both failures are silent: a
component claimed twice makes a total over-agree while every line looks right,
and an unexplained combination is indistinguishable from a reconciler tuned
until it stopped complaining.
"""

from __future__ import annotations

from datetime import date

import pytest

from audit.reconcile import Outcome, Reconciliation, Tolerance, reconcile
from audit.reconcile.report import render
from audit.statements import Statement, parse_statement
from audit.statements.mapping import MAP, LineRule, Side, check_map, normalize_label, split_side
from audit.statements.model import Section
from tariffkit.billing import Bill, BillingPeriod
from tariffkit.config import CcaConfig, Config
from tariffkit.models import Supplier

from .test_statements import load

PERIOD = BillingPeriod(date(2025, 12, 30), date(2026, 1, 29))
CONFIG = Config(
    tariff="E-TOU-C",
    supplier=Supplier.CCA,
    cca=CcaConfig(name="MCE", rate_card="mce", pcia_vintage=2011),
)


class TestMapInvariants:
    def test_the_shipped_map_is_consistent(self) -> None:
        assert check_map() == []

    def test_a_component_claimed_twice_is_rejected(self) -> None:
        # One computed dollar compared against two printed lines: the total
        # over-agrees while every individual line looks right.
        rules = (
            LineRule("A", Section.PGE_BREAKDOWN, Side.IMPORT, ("distribution",)),
            LineRule("B", Section.PGE_BREAKDOWN, Side.IMPORT, ("distribution",)),
        )
        (problem,) = check_map(rules)
        assert "claimed by both" in problem

    def test_an_unexplained_combination_is_rejected(self) -> None:
        rules = (LineRule("A", Section.PGE_BREAKDOWN, Side.IMPORT, ("one", "two")),)
        (problem,) = check_map(rules)
        assert "does not say why" in problem

    def test_a_printed_line_claimed_twice_is_rejected(self) -> None:
        rules = (
            LineRule("Distribution", Section.PGE_BREAKDOWN, Side.IMPORT, ("a",)),
            LineRule(
                "Other", Section.PGE_BREAKDOWN, Side.IMPORT, ("b",), aliases=("Distribution",)
            ),
        )
        assert any("claimed by both" in problem for problem in check_map(rules))

    def test_every_shipped_rule_records_where_it_was_confirmed(self) -> None:
        # Not an invariant of the structure -- a new rule starts unverified, and
        # that is the point -- but every rule currently shipped has reconciled
        # against a real statement, and losing that should be deliberate.
        assert [rule.label for rule in MAP if not rule.confirmed] == []

    def test_labels_fold_to_the_same_key(self) -> None:
        assert normalize_label("Competition Transition Charges (CTC)") == normalize_label(
            "competition transition charges"
        )

    def test_a_side_qualified_component_resolves(self) -> None:
        # cca_generation exists as both an import charge and an export credit,
        # so a component's side is stated rather than searched for.
        assert split_side("fixed:base_services_charge", Side.IMPORT) == (
            Side.FIXED,
            "base_services_charge",
        )
        assert split_side("distribution", Side.IMPORT) == (Side.IMPORT, "distribution")


def _bill(imports: dict[str, float], fixed: dict[str, float] | None = None) -> Bill:
    return Bill(
        period=PERIOD,
        import_components=dict(imports),
        export_components={},
        fixed_components=dict(fixed or {}),
    )


def _statement() -> Statement:
    return parse_statement(load(), source="synthetic")


class TestOutcomes:
    def test_an_agreeing_line_matches(self) -> None:
        statement = _statement()
        result = reconcile(statement, _bill({"nuclear_decommissioning": -0.05}), CONFIG)
        found = [c for c in result.comparisons if c.label == "Nuclear Decommissioning"]
        assert found and found[0].outcome is Outcome.MATCH

    def test_a_grouped_line_is_judged_on_the_whole_group(self) -> None:
        # Distribution and Public Purpose Programs are one comparison, because
        # the Base Services Charge is split across them at a ratio the utility
        # does not publish. Supplying one component of a group is not agreement.
        statement = _statement()
        partial = reconcile(statement, _bill({"public_purpose_programs": 12.00}), CONFIG)
        grouped = [c for c in partial.comparisons if "Public Purpose" in c.label]
        assert grouped and grouped[0].outcome is Outcome.MISMATCH

        whole = reconcile(
            statement,
            _bill(
                {"distribution": 80.00, "public_purpose_programs": 12.00},
                {"base_services_charge": 24.60},
            ),
            CONFIG,
        )
        grouped = [c for c in whole.comparisons if "Public Purpose" in c.label]
        assert grouped and grouped[0].outcome is Outcome.MATCH
        # The two printed lines are compared as one: 94.60 + 22.00.
        assert grouped[0].printed == pytest.approx(116.60)

    def test_a_printed_line_no_rule_claims_is_its_own_outcome(self) -> None:
        # A charge the utility introduces that the map has never seen. It must
        # not be silently dropped, or the computed total quietly stops covering
        # everything the bill charges for.
        pages = [
            page.replace(
                "     Taxes and Other",
                "     Brand New Rider                             4.00\n     Taxes and Other",
            )
            for page in load()
        ]
        result = reconcile(parse_statement(pages), _bill({}), CONFIG)
        unmapped = [c for c in result.comparisons if c.outcome is Outcome.UNMAPPED_LINE]
        assert [c.label for c in unmapped] == ["Brand New Rider"]

    def test_a_computed_component_no_rule_claims_is_reported(self) -> None:
        # The sneakiest failure: every printed line can agree while the total is
        # wrong, so this is checked explicitly rather than inferred from a total.
        statement = _statement()
        result = reconcile(statement, _bill({"some_new_rider": 4.00}), CONFIG)
        unmapped = [c for c in result.comparisons if c.outcome is Outcome.UNMAPPED_COMPONENT]
        assert [c.label for c in unmapped] == ["some_new_rider"]

    def test_a_negligible_component_is_not_reported_as_unmapped(self) -> None:
        statement = _statement()
        result = reconcile(statement, _bill({"rounding_dust": 0.001}), CONFIG)
        assert not any(c.outcome is Outcome.UNMAPPED_COMPONENT for c in result.comparisons)

    def test_a_rule_whose_components_are_absent_is_not_computed(self) -> None:
        statement = _statement()
        result = reconcile(statement, _bill({}), CONFIG)
        assert any(c.outcome is Outcome.NOT_COMPUTED for c in result.comparisons)

    def test_a_disagreeing_line_mismatches(self) -> None:
        statement = _statement()
        result = reconcile(statement, _bill({"nuclear_decommissioning": 99.00}), CONFIG)
        found = [c for c in result.comparisons if c.label == "Nuclear Decommissioning"]
        assert found and found[0].outcome is Outcome.MISMATCH


class TestTolerance:
    def test_rounding_on_a_single_line_is_allowed(self) -> None:
        assert Tolerance().line_ok(10.00, 10.005, 1)

    def test_the_allowance_grows_with_the_number_of_components(self) -> None:
        # Three components carry three independent roundings; a flat cent would
        # fail every combined line for reasons that say nothing about the rates.
        assert not Tolerance().line_ok(10.00, 10.025, 1)
        assert Tolerance().line_ok(10.00, 10.025, 3)

    def test_it_is_tight_enough_to_catch_a_misplaced_component(self) -> None:
        # The real case: reliability_services (0.15) assigned to Distribution
        # instead of Transmission. An earlier 0.001 relative allowance let this
        # pass on a $182 line, and only Transmission's matching shortfall gave
        # it away.
        assert not Tolerance().line_ok(182.21, 182.37, 3, 182.37)
        assert Tolerance().line_ok(182.21, 182.22, 2, 182.22)

    def test_offsetting_components_are_judged_on_their_gross(self) -> None:
        # Conservation Incentive nets 63.09 against -28.87 and prints 34.25. The
        # noise rides on the 91.96 of energy priced, not on what is left after
        # they cancel.
        assert not Tolerance().line_ok(34.25, 34.22, 2)
        assert Tolerance().line_ok(34.25, 34.22, 2, 91.96)


class TestReport:
    def test_it_says_whether_it_reconciled(self) -> None:
        statement = _statement()
        result = reconcile(statement, _bill({"public_purpose_programs": 12.00}), CONFIG)
        assert "RECONCILED" in render(result) or "FAILED" in render(result)

    def test_a_bill_that_calls_itself_incomplete_is_flagged(self) -> None:
        # A total missing a charge is indistinguishable from a correct one
        # unless the report says so.
        statement = _statement()
        bill = Bill(period=PERIOD, import_components={}, complete=False)
        rendered = render(reconcile(statement, bill, CONFIG))
        assert "incomplete" in rendered

    def test_an_empty_reconciliation_is_renderable(self) -> None:
        statement = _statement()
        result = Reconciliation(statement=statement, bill=_bill({}), config=CONFIG)
        assert "billed" in render(result)


class TestPresentationsAreNotDoubleCounted:
    def test_the_delivery_detail_is_skipped_when_the_breakdown_exists(self) -> None:
        # The two sections restate the same money in different arrangements.
        # Comparing both would count every component twice.
        statement = _statement()
        result = reconcile(statement, _bill({"pcia": 10.00}), CONFIG)
        sections = {c.section for c in result.comparisons}
        assert Section.PGE_DELIVERY not in sections

    def test_pcia_is_compared_once(self) -> None:
        statement = _statement()
        result = reconcile(statement, _bill({"pcia": 10.00}), CONFIG)
        assert len([c for c in result.comparisons if c.label == "PCIA"]) == 1
