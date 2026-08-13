"""Resolving which configuration priced a cycle.

The two behaviours worth pinning are both refusals, because both replace a
believable wrong answer with an obvious failure.
"""

from __future__ import annotations

from datetime import date

import pytest

from audit.account import (
    AccountHistory,
    check_against_statement,
    schedule_from_printed,
)
from audit.errors import AccountError
from audit.statements import parse_statement
from nem_rates.billing import BillingPeriod
from nem_rates.models import Supplier

from .test_statements import load

RAW = {
    "base": {
        "utility": "PGE",
        "interconnection_year": 2026,
        "pto_date": "2026-06-03",
        "baseline_territory": "X",
    },
    "epoch": [
        {
            "from": date(2025, 1, 1),
            "tariff": "E-TOU-C",
            "supplier": "cca",
            "cca": {"name": "MCE", "rate_card": "mce", "pcia_vintage": 2011},
            "note": "MCE generation",
        },
        {
            "from": date(2026, 3, 1),
            "tariff": "EV2-A",
            "supplier": "cca",
            "cca": {"name": "MCE", "rate_card": "mce", "pcia_vintage": 2011},
            "note": "moved to EV2-A",
        },
    ],
}


@pytest.fixture
def history() -> AccountHistory:
    return AccountHistory.from_dict(RAW)


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
        # Nothing on a statement states the tariff code, so the correspondence
        # between what is printed and what this library calls it is written down
        # rather than inferred.
        assert schedule_from_printed(printed) == expected

    def test_an_unknown_name_is_not_guessed(self) -> None:
        assert schedule_from_printed("Some New Pilot Rate") is None


class TestConfigForACycle:
    def test_the_epoch_in_force_is_used(self, history: AccountHistory) -> None:
        config = history.config_for(BillingPeriod(date(2025, 12, 30), date(2026, 1, 29)))
        assert config.tariff == "E-TOU-C"
        assert config.supplier is Supplier.CCA
        assert config.cca is not None and config.cca.rate_card == "mce"
        # The base survives where an epoch does not override it.
        assert config.baseline_territory == "X"
        assert config.pto_date == date(2026, 6, 3)

    def test_a_later_cycle_gets_the_later_epoch(self, history: AccountHistory) -> None:
        config = history.config_for(BillingPeriod(date(2026, 4, 1), date(2026, 4, 29)))
        assert config.tariff == "EV2-A"

    def test_a_cycle_spanning_a_change_is_split_not_refused(self, history: AccountHistory) -> None:
        # The account changed mid-cycle, so two configurations priced it -- one
        # each side of the change, which is what the utility itself does. This
        # used to raise; refusing skipped exactly the cycles worth checking
        # hardest and left only the quiet months verified.
        segments = history.segments_for(BillingPeriod(date(2026, 2, 20), date(2026, 3, 20)))
        assert [(s.period.start, s.period.end) for s in segments] == [
            (date(2026, 2, 20), date(2026, 2, 28)),
            (date(2026, 3, 1), date(2026, 3, 20)),
        ]
        # Different tariffs, and the segments tile the cycle without overlap.
        assert segments[0].config.tariff != segments[1].config.tariff
        assert sum(s.period.days for s in segments) == 29

    def test_a_cycle_before_every_epoch_is_refused(self, history: AccountHistory) -> None:
        with pytest.raises(AccountError, match="no account epoch covers"):
            history.config_for(BillingPeriod(date(2024, 1, 1), date(2024, 1, 31)))

    def test_the_boundary_day_itself_belongs_to_the_new_epoch(
        self, history: AccountHistory
    ) -> None:
        assert history.config_for(BillingPeriod(date(2026, 3, 1), date(2026, 3, 31))).tariff == (
            "EV2-A"
        )

    def test_an_epoch_needs_a_date(self) -> None:
        with pytest.raises(AccountError, match="needs a 'from' date"):
            AccountHistory.from_dict({"base": {}, "epoch": [{"tariff": "E-ELEC"}]})

    def test_a_missing_file_says_what_to_copy(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(AccountError, match=r"account\.example\.toml"):
            AccountHistory.from_toml(tmp_path / "nope.toml")

    def test_the_shipped_example_parses(self) -> None:
        # It is the template a user copies, so it has to be valid.
        from pathlib import Path

        history = AccountHistory.from_toml(Path(__file__).parent.parent / "account.example.toml")
        assert history.config_for(BillingPeriod(date(2026, 1, 1), date(2026, 1, 29))).tariff == (
            "E-TOU-C"
        )


class TestStatementConfirmsTheEpoch:
    """The bill is asked whether the configuration describes it."""

    def test_a_matching_configuration_reports_nothing(self, history: AccountHistory) -> None:
        statement = parse_statement(load())
        config = history.config_for(statement.period)
        assert check_against_statement(config, statement) == []

    def test_the_wrong_schedule_is_caught(self, history: AccountHistory) -> None:
        statement = parse_statement(load())
        config = history.config_for(statement.period).with_(tariff="EV2-A")
        (problem,) = check_against_statement(config, statement)
        assert "'EV2-A'" in problem and "'E-TOU-C'" in problem

    def test_the_wrong_pcia_vintage_is_caught(self, history: AccountHistory) -> None:
        # The statement prints its own vintage, and the PCIA differs threefold
        # across vintages, so this is worth several dollars a month.
        from nem_rates.config import CcaConfig

        statement = parse_statement(load())
        base = history.config_for(statement.period)
        config = base.with_(cca=CcaConfig(name="MCE", rate_card="mce", pcia_vintage=2009))
        (problem,) = check_against_statement(config, statement)
        assert "2009" in problem and "2011" in problem

    def test_pricing_a_cca_account_as_bundled_is_caught(self, history: AccountHistory) -> None:
        # This exact misconfiguration shipped once and was worth ~14% of the
        # generation charge, so it gets its own test.
        statement = parse_statement(load())
        config = history.config_for(statement.period).with_(supplier=Supplier.BUNDLED, cca=None)
        problems = check_against_statement(config, statement)
        assert any("MCE supplied generation" in problem for problem in problems)

    def test_an_unrecognised_printed_schedule_is_reported_not_ignored(
        self, history: AccountHistory
    ) -> None:
        # A check that cannot be performed must not look like a check that
        # passed.
        # Both names, because the statement prints the utility's marketing name
        # and the CCA's tariff code, and either one being recognised means the
        # check *can* be performed.
        pages = [
            page.replace(
                "Time-of-Use (Peak Pricing 4 - 9 p.m. Every Day)", "Mystery Rate Plan"
            ).replace("ETOUC", "MYSTERY")
            for page in load()
        ]
        statement = parse_statement(pages)
        config = history.config_for(statement.period)
        (problem,) = check_against_statement(config, statement)
        assert "matches a known tariff" in problem
