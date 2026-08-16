"""Audit-only validation of public account segments against statements."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from audit.reconcile.account import check_against_statement, schedule_from_printed
from tariffkit.account import AccountEpoch, AccountError, AccountProfile
from tariffkit.billing import BillingPeriod
from tariffkit.config import CcaConfig, Config
from tariffkit.models import Supplier
from tariffkit.providers.pge.statements import StatementAmbiguityError, parse_statement

from .test_statements import load


def profile() -> AccountProfile:
    cca = CcaConfig(name="MCE", rate_card="mce", pcia_vintage=2011)
    return AccountProfile(
        (
            AccountEpoch(
                date(2025, 1, 1),
                Config(
                    tariff="E-TOU-C",
                    supplier=Supplier.CCA,
                    cca=cca,
                    interconnection_year=2026,
                    pto_date=date(2026, 6, 3),
                    baseline_territory="X",
                ),
                "MCE generation",
            ),
            AccountEpoch(
                date(2026, 3, 1),
                Config(
                    tariff="EV2-A",
                    supplier=Supplier.CCA,
                    cca=cca,
                    interconnection_year=2026,
                    pto_date=date(2026, 6, 3),
                    baseline_territory="X",
                ),
                "moved to EV2-A",
            ),
        ),
        name="test",
    )


class TestPrintedSchedules:
    @pytest.mark.parametrize(
        ("printed", "expected"),
        [
            ("Time-of-Use (Peak Pricing 4 - 9 p.m. Every Day)", "E-TOU-C"),
            ("EV2A  Home Charging", "EV2-A"),
            ("EV2-A", "EV2-A"),
            ("Electric Home Rate Plan", "E-ELEC"),
        ],
    )
    def test_marketing_names_map_to_tariffs(self, printed: str, expected: str) -> None:
        assert schedule_from_printed(printed) == expected

    def test_an_unknown_name_is_not_guessed(self) -> None:
        assert schedule_from_printed("Some New Pilot Rate") is None


class TestPublicProfileSegments:
    def test_the_epoch_in_force_is_used(self) -> None:
        account = profile()
        config = account.config_at(date(2025, 12, 30))
        assert config.tariff == "E-TOU-C"
        assert config.supplier is Supplier.CCA
        assert config.cca is not None and config.cca.rate_card == "mce"
        assert config.baseline_territory == "X"
        assert config.pto_date == date(2026, 6, 3)

    def test_a_later_cycle_gets_the_later_epoch(self) -> None:
        assert profile().config_at(date(2026, 4, 1)).tariff == "EV2-A"

    def test_a_cycle_spanning_a_change_is_split(self) -> None:
        segments = profile().segments_for(BillingPeriod(date(2026, 2, 20), date(2026, 3, 20)))
        assert [(s.period.start, s.period.end) for s in segments] == [
            (date(2026, 2, 20), date(2026, 2, 28)),
            (date(2026, 3, 1), date(2026, 3, 20)),
        ]
        assert segments[0].config.tariff != segments[1].config.tariff
        assert sum(s.period.days for s in segments) == 29

    def test_a_cycle_before_every_epoch_is_refused(self) -> None:
        with pytest.raises(AccountError, match="before the first account epoch"):
            profile().config_at(date(2024, 1, 1))


class TestStatementConfirmsTheProfile:
    def test_a_matching_configuration_reports_nothing(self) -> None:
        statement = parse_statement(load())
        account = profile()
        segments = account.segments_for(statement.period)
        assert check_against_statement(segments[-1].config, statement, segments=segments) == []

    def test_the_exact_statement_spans_are_required(self) -> None:
        statement = parse_statement(load())
        account = profile()
        segments = account.segments_for(statement.period)
        shortened = replace(
            statement,
            agreements=(
                replace(
                    statement.agreements[0],
                    period=BillingPeriod(statement.period.start, date(2026, 1, 10)),
                ),
            ),
        )
        problems = check_against_statement(segments[-1].config, shortened, segments=segments)
        assert any("spans" in problem and "profile segments" in problem for problem in problems)

    def test_the_wrong_schedule_is_caught(self) -> None:
        statement = parse_statement(load())
        account = profile()
        segments = account.segments_for(statement.period)
        config = segments[-1].config.with_(tariff="EV2-A")
        (problem,) = check_against_statement(config, statement)
        assert "'EV2-A'" in problem and "'E-TOU-C'" in problem

    def test_the_wrong_pcia_vintage_is_caught(self) -> None:
        statement = parse_statement(load())
        account = profile()
        segments = account.segments_for(statement.period)
        config = segments[-1].config.with_(
            cca=CcaConfig(name="MCE", rate_card="mce", pcia_vintage=2009)
        )
        (problem,) = check_against_statement(config, statement)
        assert "2009" in problem and "2011" in problem

    def test_pricing_a_cca_account_as_bundled_is_caught(self) -> None:
        statement = parse_statement(load())
        account = profile()
        segments = account.segments_for(statement.period)
        config = segments[-1].config.with_(supplier=Supplier.BUNDLED, cca=None)
        problems = check_against_statement(config, statement)
        assert any("MCE supplied generation" in problem for problem in problems)

    def test_an_unrecognised_printed_schedule_is_reported(self) -> None:
        pages = [
            page.replace(
                "Time-of-Use (Peak Pricing 4 - 9 p.m. Every Day)", "Mystery Rate Plan"
            ).replace("ETOUC", "MYSTERY")
            for page in load()
        ]
        with pytest.raises(StatementAmbiguityError, match="unsupported tariff"):
            parse_statement(pages)
