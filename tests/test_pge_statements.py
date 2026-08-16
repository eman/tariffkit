from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tariffkit.billing import BillingPeriod
from tariffkit.providers.pge.statements import (
    StatementAmbiguityError,
    StatementError,
    normalize_tariff,
    parse_statement,
)

FIXTURE = Path(__file__).parent / "fixtures" / "statements" / "synthetic_cca_ratechange.txt"


def load_fixture() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").split("\x0c")


def test_statement_maps_exact_delivery_span_to_same_page_tariff() -> None:
    statement = parse_statement(load_fixture())

    assert len(statement.agreements) == 1
    agreement = statement.agreements[0]
    assert agreement.period == BillingPeriod(date(2025, 12, 30), date(2026, 1, 29))
    assert agreement.printed_schedule.startswith("Time-of-Use")
    assert agreement.tariff == "E-TOU-C"
    assert agreement.page == 2
    assert agreement.account_masked == "9999"
    assert agreement.baseline_territory == "X"
    assert agreement.pcia_vintage == 2011
    assert statement.self_check() == []


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("Time-of-Use (Peak Pricing 4 - 9 p.m. Every Day)", "E-TOU-C"),
        ("EV2-A", "EV2-A"),
        ("Schedule E-ELEC", "E-ELEC"),
        ("unknown schedule", None),
    ],
)
def test_normalize_tariff(printed: str, expected: str | None) -> None:
    assert normalize_tariff(printed) == expected


def test_parse_errors_are_public_statement_errors() -> None:
    with pytest.raises(StatementError, match="no statement date"):
        parse_statement(["not a statement"])


def minimal_statement(*delivery_pages: str) -> list[str]:
    return [
        "\n".join(
            [
                "Statement Date: 02/05/2026",
                "Your Account Summary",
                "Total Amount Due                                      10.00",
                *delivery_pages,
            ]
        )
    ]


def test_independent_spans_and_schedules_are_ambiguous() -> None:
    pages = minimal_statement(
        "Details of PG&E Electric Delivery Charges",
        "01/01/2026 to 01/02/2026 (2 billing days)",
        "02/03/2026 to 02/10/2026 (8 billing days)",
        "Rate Schedule: E-ELEC",
        "Total PG&E Electric Delivery Charges                     10.00",
    )

    with pytest.raises(StatementAmbiguityError) as raised:
        parse_statement(pages)

    assert raised.value.diagnostics == ("page 1 prints 2 date spans for one delivery schedule",)


def test_conflicting_same_span_evidence_is_ambiguous() -> None:
    pages = [
        "\n".join(
            [
                "Statement Date: 02/05/2026",
                "Your Account Summary",
                "Total Amount Due                                      10.00",
            ]
        ),
        "\n".join(
            [
                "Details of PG&E Electric Delivery Charges",
                "01/01/2026 to 01/10/2026 (10 billing days)",
                "Rate Schedule: E-ELEC",
                "Total PG&E Electric Delivery Charges                      5.00",
            ]
        ),
        "\n".join(
            [
                "Details of PG&E Electric Delivery Charges",
                "01/01/2026 to 01/10/2026 (10 billing days)",
                "Rate Schedule: EV2-A",
                "Total PG&E Electric Delivery Charges                      5.00",
            ]
        ),
    ]

    with pytest.raises(StatementAmbiguityError, match="conflicting delivery schedules"):
        parse_statement(pages)


def test_delivery_heading_without_local_agreement_evidence_is_ambiguous() -> None:
    pages = [
        minimal_statement("01/01/2026 to 02/05/2026 (36 billing days)")[0],
        "\n".join(
            [
                "Details of PG&E Electric Delivery Charges",
                "A delivery page with no schedule or exact service span",
                "Total PG&E Electric Delivery Charges                     10.00",
            ]
        ),
    ]

    with pytest.raises(StatementAmbiguityError, match="no exact date span"):
        parse_statement(pages)
