"""Pricing a statement from its own metered quantities.

The verdict this produces is the audit's central claim on any mismatch -- the
rates, or the energy? -- so the ways it can be confidently wrong matter more
than the ways it can fail.
"""

from __future__ import annotations

from datetime import date

import pytest

from audit.reconcile.attribution import _tou, fixed_from_statement, priced_from_statement
from tariffkit.billing import BillingPeriod
from tariffkit.config import CcaConfig, Config
from tariffkit.models import Supplier, TouPeriod
from tariffkit.providers.pge.statements import Section, Statement, StatementLine, StatementSection

PERIOD = BillingPeriod(date(2026, 6, 30), date(2026, 7, 28))


def config() -> Config:
    return Config(
        utility="PGE",
        tariff="E-ELEC",
        supplier=Supplier.CCA,
        cca=CcaConfig(name="MCE", rate_card="mce", pcia_vintage=2011),
        baseline_territory="X",
        pto_date=date(2026, 6, 3),
    )


def row(label: str, kwh: float, block: str = "") -> StatementLine:
    return StatementLine(
        label=label,
        amount=1.0,
        section=Section.PGE_DELIVERY,
        page=1,
        quantity=kwh,
        unit="kWh",
        block=block,
    )


def statement(lines: tuple[StatementLine, ...]) -> Statement:
    return Statement(
        statement_date=date(2026, 8, 4),
        period=PERIOD,
        amount_due=25.48,
        sections=(StatementSection(name=Section.PGE_DELIVERY, lines=lines, printed_total=25.48),),
    )


class TestSolarBillingPlanRows:
    """Once solar is interconnected the same labels print twice, on both sides."""

    def test_produced_rows_are_not_priced_as_imports(self) -> None:
        # The delivery page carries Peak/Part Peak/Off Peak under both "Energy
        # Produced" and "Energy Delivered". Summing both doubles the import
        # reconstruction, and the verdict then reports that the rates do not
        # reproduce the line -- the exact inverse of the truth.
        both = statement(
            (
                row("Peak", 10.0, block="Energy Produced"),
                row("Off Peak", 40.0, block="Energy Produced"),
                row("Peak", 10.0, block="Energy Delivered"),
                row("Off Peak", 40.0, block="Energy Delivered"),
            )
        )
        delivered_only = statement(
            (
                row("Peak", 10.0, block="Energy Delivered"),
                row("Off Peak", 40.0, block="Energy Delivered"),
            )
        )
        assert priced_from_statement(both, config()) == pytest.approx(
            priced_from_statement(delivered_only, config())
        )

    def test_a_statement_without_blocks_is_unaffected(self) -> None:
        # Everything before interconnection prints no sub-headings at all.
        plain = statement((row("Peak", 10.0), row("Off Peak", 40.0)))
        assert priced_from_statement(plain, config())["distribution"] > 0


class TestLabels:
    @pytest.mark.parametrize(
        ("label", "period", "season"),
        [
            ("Peak", TouPeriod.PEAK, None),
            ("Off Peak", TouPeriod.OFF_PEAK, None),
            ("Part Peak", TouPeriod.PART_PEAK, None),
            ("Off Peak Summer", TouPeriod.OFF_PEAK, "summer"),
            ("Peak Winter", TouPeriod.PEAK, "winter"),
        ],
    )
    def test_the_season_a_label_names_is_kept(
        self, label: str, period: TouPeriod, season: str | None
    ) -> None:
        # The provider's page names the season rather than the dates, and a
        # cycle can span the boundary. Pricing a winter row on a summer day is
        # a rate error wearing a reconciliation failure's clothes.
        assert _tou(label) == (period, season)

    def test_a_charge_row_is_not_a_time_of_use_row(self) -> None:
        assert _tou("Generation Credit") is None


class TestFixedCharges:
    def test_keys_carry_their_side(self) -> None:
        # A rule names "fixed:base_services_charge"; the same word means
        # something different on other sides, so the lookup is namespaced.
        line = StatementLine(
            label="Base Services Charge", amount=23.01, section=Section.PGE_DELIVERY, page=1
        )
        assert fixed_from_statement(statement((line,))) == {"fixed:base_services_charge": 23.01}
