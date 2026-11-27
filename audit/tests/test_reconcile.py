"""The map's invariants, and the comparison built on them.

``reconcile`` is pure, so everything here runs on constructed bills. The map
invariants are tested rather than trusted because both failures are silent: a
component claimed twice makes a total over-agree while every line looks right,
and an unexplained combination is indistinguishable from a reconciler tuned
until it stopped complaining.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import pytest

from audit.reconcile import Outcome, Reconciliation, Tolerance, reconcile
from audit.reconcile.compare import unclaimed_components
from audit.reconcile.mapping import MAP, LineRule, Side, check_map, normalize_label, split_side
from audit.reconcile.report import render
from tariffkit.billing import Bill, BillingPeriod, IntervalReading
from tariffkit.config import CcaConfig, Config
from tariffkit.models import Supplier
from tariffkit.providers.pge.statements import Section, Statement, parse_statement

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

    def test_the_map_accounts_for_every_component_the_engine_emits(self) -> None:
        """Every rule here is fed by the engine, so the map has to keep up.

        The rest of this file builds bills by hand, which is why adding a real
        export component to a CCA account once slipped through: the fixtures
        never grew it, while `audit reconcile` -- pricing a real statement --
        reported it unmapped on every MCE cycle and exited non-zero. Pricing an
        hour through the engine is what makes that visible here.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from tariffkit import RateEngine

        config = Config(
            tariff="E-ELEC",
            supplier=Supplier.CCA,
            cca=CcaConfig(name="MCE", rate_card="mce"),
            interconnection_year=2026,
            pto_date=date(2026, 1, 1),
        )
        priced = RateEngine(config).price_at(
            datetime(2026, 7, 15, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
        )
        bill = Bill(
            period=BillingPeriod(date(2026, 6, 30), date(2026, 7, 28)),
            export_components={k: -v for k, v in priced.export_price.components.items()},
        )
        assert unclaimed_components(bill) == {}

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


def test_a_line_the_rate_table_reproduces_is_still_a_mismatch() -> None:
    """The independent figure checks the rate table, not the engine.

    It is `rate x the statement's own kWh`, and never passes through
    `BillEngine`. So a defect in how the engine applies that rate leaves the
    two identical, and treating "the rates reproduce this line" as an excuse
    made the check unable to fail on anything the rate table could reproduce:
    a $1,152.36 error on a $295.30 bill reconciled clean.
    """
    from audit.reconcile.compare import Comparison, Outcome

    reproduced = Comparison(
        "PCIA",
        MAP[0].section,
        Outcome.MISMATCH,
        printed=11.64,
        computed=1164.00,
        from_statement=11.64,
    )

    assert reproduced.rates_agree is True
    assert reproduced.ok is False
    assert not hasattr(Outcome, "METERED")


def _hour(day: int, hour: int, kwh: float) -> IntervalReading:
    from datetime import datetime

    from tariffkit.timeutil import PACIFIC

    return IntervalReading(
        start=datetime(2026, 1, day, hour, tzinfo=PACIFIC),
        imported=kwh,
        exported=0.0,
        duration=timedelta(hours=1),
    )


def test_the_time_of_use_split_is_asserted_on_above_the_source_noise() -> None:
    """Two derivations disagreeing about *which hour* is real money.

    Asserted, because a cycle's worth of misattributed peak energy is worth
    dollars, and above a floor, because the reference rounds every interval to
    two decimals and its own quantisation accumulates ~0.31 kWh over a cycle.
    """
    from audit.sources import compare_sources
    from tariffkit.providers.pge.statements.model import Statement

    classify = lambda moment: "peak" if 16 <= moment.hour < 21 else "off_peak"  # noqa: E731
    statement = Statement(
        statement_date=date(2026, 2, 1),
        period=BillingPeriod(date(2026, 1, 1), date(2026, 1, 2)),
        amount_due=0.0,
    )

    def deltas(other_peak_kwh: float) -> list[Any]:
        return [
            d
            for d in compare_sources(
                {
                    "influx": [_hour(1, 17, 10.0)],
                    "green_button": [_hour(1, 17, other_peak_kwh)],
                },
                statement,
                classify=classify,
            )
            if d.left.endswith("peak")
        ]

    # Below the reference's own rounding noise: reported, not asserted.
    (small,) = deltas(10.06)
    assert small.significant is False
    # A cycle's worth of misattribution: asserted.
    (large,) = deltas(14.05)
    assert large.significant is True


def test_applied_credit_is_reconciled_from_the_bank_the_statement_opened_with() -> None:
    """2026-10-05 spent $11.96 carried in plus what the cycle earned.

    Against an empty bank only the cycle's own credit could be applied, and a
    statement whose rates were right read as a $12 overcharge.
    """
    from dataclasses import replace

    from tariffkit.billing import CreditBalances
    from tariffkit.providers.pge.statements.model import StatementLine, StatementSection

    section = Section.CCA_GENERATION
    statement = Statement(
        statement_date=date(2026, 10, 5),
        period=PERIOD,
        amount_due=0.0,
        sections=(
            StatementSection(
                section,
                (
                    StatementLine("Energy Export Credits Applied", -18.70, section, 5),
                    StatementLine("Energy Export Bonus Credits Applied", 0.0, section, 5),
                ),
            ),
        ),
    )
    bill = Bill(
        period=PERIOD,
        import_components={"cca_generation": 18.70},
        export_components={"cca_generation": -8.70},
    )

    def applied(opening: CreditBalances | None) -> Any:
        result = reconcile(replace(statement, opening_bank=opening), bill, CONFIG)
        (line,) = [c for c in result.comparisons if c.label == "Energy Export Credits Applied"]
        return line

    carried = applied(CreditBalances(generation=10.0))
    assert carried.outcome is Outcome.MATCH, carried
    assert carried.computed == pytest.approx(-18.70)

    empty = applied(None)
    assert empty.outcome is Outcome.MISMATCH
    assert empty.computed == pytest.approx(-8.70)


def test_a_meter_that_disagrees_with_the_statement_about_export_is_flagged() -> None:
    """Imports alone were checked, so a meter that lost export passed.

    Export is the side every credit is priced from.
    """
    from audit.sources import compare_sources

    def against(billed_export: float) -> Any:
        statement = Statement(
            statement_date=date(2026, 2, 1),
            period=BillingPeriod(date(2026, 1, 1), date(2026, 1, 1)),
            amount_due=0.0,
            billed_kwh=10.0,
            billed_export_kwh=billed_export,
        )
        meter = [
            IntervalReading(
                start=_hour(1, 12, 0.0).start,
                imported=10.0,
                exported=20.0,
                duration=timedelta(hours=1),
            )
        ]
        (delta,) = [
            d for d in compare_sources({"influx": meter}, statement) if d.left == "statement"
        ]
        return delta

    assert against(20.0).significant is False
    flagged = against(30.0)
    assert flagged.significant is True
    assert flagged.exported_delta == pytest.approx(10.0)


def test_a_line_read_off_the_statement_gets_no_verdict_about_the_rates() -> None:
    """An applied-credit line is the statement's own figure, not a priced one.

    "Energy Export Credits Applied" is made only of credits the statement says
    it applied, so pricing it "from the statement" read the printed line back
    and found it reproduced, on every mismatch -- a verdict about rates on a
    line no rate went into. The 2026-10-05 statement printed it that way.
    """
    from tariffkit.providers.pge.statements.model import StatementLine, StatementSection

    section = Section.CCA_GENERATION
    statement = Statement(
        statement_date=date(2026, 10, 5),
        period=PERIOD,
        amount_due=0.0,
        sections=(
            StatementSection(
                section,
                (StatementLine("Energy Export Credits Applied", -18.70, section, 5),),
            ),
        ),
    )
    bill = Bill(
        period=PERIOD,
        import_components={"cca_generation": 18.75},
        export_components={"cca_generation": -40.0},
    )

    (line,) = [
        c
        for c in reconcile(statement, bill, CONFIG).comparisons
        if c.label == "Energy Export Credits Applied"
    ]
    assert line.outcome is Outcome.MISMATCH
    assert line.from_statement is None
    assert line.rates_agree is None
    assert "priced from the statement" not in render(
        reconcile(statement, bill, CONFIG), verbose=False
    )


class TestPricedFromGreenButton:
    """The utility's export as the source a bill is priced from.

    Its rounding then shows up from the other side: InfluxDB and the statement
    read a little *high* against it, which is the same shortfall the
    comparison already allows when Green Button is only the comparison.
    """

    @staticmethod
    def _statement(billed_kwh: float | None = None) -> Statement:
        return Statement(
            statement_date=date(2026, 2, 1),
            period=BillingPeriod(date(2026, 1, 1), date(2026, 1, 1)),
            amount_due=0.0,
            billed_kwh=billed_kwh,
        )

    @staticmethod
    def _quarters(kwh_each: float, count: int = 96) -> list[IntervalReading]:
        from datetime import datetime

        from tariffkit.timeutil import PACIFIC

        start = datetime(2026, 1, 1, tzinfo=PACIFIC)
        return [
            IntervalReading(
                start=start + timedelta(minutes=15 * i),
                imported=kwh_each,
                duration=timedelta(minutes=15),
            )
            for i in range(count)
        ]

    def _against_influx(self, influx_kwh: float) -> Any:
        from audit.sources import GREEN_BUTTON, compare_sources

        # 96 quarter-hours of 0.10 kWh: 9.6 kWh, with up to 0.48 kWh of rounding.
        (delta,) = [
            d
            for d in compare_sources(
                {GREEN_BUTTON: self._quarters(0.10), "influx": [_hour(1, 12, influx_kwh)]},
                self._statement(),
                primary=GREEN_BUTTON,
            )
            if d.left == "influx"
        ]
        return delta

    def test_another_source_reading_high_by_the_rounding_is_expected(self) -> None:
        # 0.45 kWh is past the 0.35 floor, so only the rounding allowance passes it.
        delta = self._against_influx(9.6 + 0.45)
        assert delta.significant is False
        assert "priced this bill" in delta.note

    def test_another_source_reading_high_by_more_than_rounding_is_flagged(self) -> None:
        assert self._against_influx(9.6 + 0.6).significant is True

    def test_another_source_reading_low_gets_no_allowance(self) -> None:
        # Rounding only loses energy, so a source *below* the export is not it.
        delta = self._against_influx(9.6 - 0.45)
        assert delta.significant is True
        assert delta.note == ""

    def test_the_statement_reading_high_by_the_rounding_is_expected(self) -> None:
        from audit.sources import GREEN_BUTTON, compare_sources

        def against(billed: float) -> Any:
            (delta,) = [
                d
                for d in compare_sources(
                    {GREEN_BUTTON: self._quarters(0.10)},
                    self._statement(billed),
                    primary=GREEN_BUTTON,
                )
                if d.left == "statement"
            ]
            return delta

        assert against(9.6 + 0.45).significant is False
        assert against(9.6 + 0.6).significant is True
        assert against(9.6 - 0.45).significant is True

    def test_the_statement_gets_no_rounding_allowance_against_influx(self) -> None:
        from audit.sources import compare_sources

        (delta,) = [
            d
            for d in compare_sources({"influx": self._quarters(0.10)}, self._statement(9.6 + 0.45))
            if d.left == "statement"
        ]
        assert delta.significant is True


def test_an_export_wider_than_the_cycle_is_clipped_to_it() -> None:
    """A wider cached file serves a narrower cycle; its totals must not."""
    from audit.sources import window, within

    start, end = window(BillingPeriod(date(2026, 1, 2), date(2026, 1, 2)))
    readings = [_hour(1, 23, 1.0), _hour(2, 0, 2.0), _hour(2, 23, 3.0), _hour(3, 0, 4.0)]
    assert [r.imported for r in within(readings, start, end)] == [2.0, 3.0]


class TestReshaped:
    """The export's timing at another source's totals."""

    @staticmethod
    def _reading(hour: int, imported: float, exported: float) -> IntervalReading:
        return replace(_hour(1, hour, imported), exported=exported)

    def test_totals_come_from_the_reference_and_timing_from_the_shape(self) -> None:
        from audit.sources import reshaped, totals

        shape = [self._reading(12, 1.0, 3.0), self._reading(18, 3.0, 1.0)]
        reference = [self._reading(0, 8.0, 2.0)]
        result = reshaped(shape, reference)
        assert totals(result) == pytest.approx((8.0, 2.0))
        # Import and export scale independently, and each keeps its own split.
        assert [r.imported for r in result] == pytest.approx([2.0, 6.0])
        assert [r.exported for r in result] == pytest.approx([1.5, 0.5])
        assert [r.start for r in result] == [r.start for r in shape]

    def test_a_direction_the_shape_lacks_is_not_invented(self) -> None:
        from audit.sources import reshaped, totals

        result = reshaped([self._reading(12, 1.0, 0.0)], [self._reading(0, 2.0, 5.0)])
        assert totals(result) == pytest.approx((2.0, 0.0))


class TestReviewFindings:
    """The rounding allowance is bounded in every direction it is applied."""

    @staticmethod
    def _statement() -> Statement:
        return Statement(
            statement_date=date(2026, 2, 1),
            period=BillingPeriod(date(2026, 1, 1), date(2026, 1, 5)),
            amount_due=0.0,
        )

    @staticmethod
    def _quarters(
        hours: range, kwh_each: float, days: int = 5, exported: float = 0.0
    ) -> list[IntervalReading]:
        from datetime import datetime

        from tariffkit.timeutil import PACIFIC

        return [
            IntervalReading(
                start=datetime(2026, 1, 1 + day, hour, 15 * q, tzinfo=PACIFIC),
                imported=kwh_each,
                exported=exported,
                duration=timedelta(minutes=15),
            )
            for day in range(days)
            for hour in hours
            for q in range(4)
        ]

    def test_a_source_that_lost_its_export_is_flagged_despite_agreeing_on_import(self) -> None:
        from audit.sources import GREEN_BUTTON, compare_sources

        exporting = self._quarters(range(0, 24), 0.0, days=1, exported=0.2)
        priced = [replace(r, imported=0.10) for r in exporting]
        # Import within the rounding allowance, every exported kWh missing.
        lost = [replace(r, imported=0.10 + 0.45 / 96, exported=0.0) for r in exporting]
        (delta,) = [
            d
            for d in compare_sources(
                {GREEN_BUTTON: priced, "influx": lost}, self._statement(), primary=GREEN_BUTTON
            )
            if d.left == "influx"
        ]
        assert delta.significant is True
        assert delta.note == ""

    @staticmethod
    def _peak(moment: Any) -> str:
        return "peak" if 16 <= moment.hour < 21 else "off_peak"

    def _peak_delta(self, influx_peak_each: float, off_peak_hours: range) -> Any:
        from audit.sources import GREEN_BUTTON, compare_sources

        # 100 peak quarter-hours at 0.10 kWh: 10 kWh, up to 0.5 kWh of rounding.
        export = self._quarters(range(16, 21), 0.10) + self._quarters(off_peak_hours, 0.10)
        influx = self._quarters(range(16, 21), influx_peak_each) + self._quarters(
            off_peak_hours, 0.10
        )
        (delta,) = [
            d
            for d in compare_sources(
                {GREEN_BUTTON: export, "influx": influx},
                self._statement(),
                primary=GREEN_BUTTON,
                classify=self._peak,
            )
            if d.left == "influx peak"
        ]
        return delta

    def test_peak_rounding_accepted_by_the_totals_is_accepted_for_peak(self) -> None:
        # 0.4 kWh: past the 0.35 floor, inside 100 peak intervals' rounding.
        delta = self._peak_delta(0.104, range(0))
        assert delta.significant is False
        assert "rounding of peak hours" in delta.note

    def test_peak_beyond_rounding_is_still_flagged(self) -> None:
        assert self._peak_delta(0.106, range(0)).significant is True

    def test_only_peak_intervals_count_toward_the_peak_allowance(self) -> None:
        from audit.sources import GREEN_BUTTON, compare_sources

        # 20 peak quarter-hours can lose 0.1 kWh; 380 off-peak ones must not
        # widen that to cover a 0.4 kWh peak difference.
        export = self._quarters(range(16, 21), 0.10, days=1) + self._quarters(
            range(0, 16), 0.10, days=5
        )
        influx = self._quarters(range(16, 21), 0.12, days=1) + self._quarters(
            range(0, 16), 0.10, days=5
        )
        (delta,) = [
            d
            for d in compare_sources(
                {GREEN_BUTTON: export, "influx": influx},
                self._statement(),
                primary=GREEN_BUTTON,
                classify=self._peak,
            )
            if d.left == "influx peak"
        ]
        assert delta.significant is True


@pytest.mark.parametrize(
    ("read_hour", "last"),
    [(0, date(2026, 1, 5)), (14, date(2026, 1, 6))],
)
def test_the_export_is_asked_for_every_date_the_cycle_reaches(read_hour: int, last: date) -> None:
    """A meter read at 14:00 ends the cycle partway into the next day."""
    from audit.sources import days_touched, window

    start, end = window(BillingPeriod(date(2026, 1, 1), date(2026, 1, 5)), read_hour=read_hour)
    assert days_touched(start, end) == (date(2026, 1, 1), last)
