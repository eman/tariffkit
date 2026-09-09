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
from tariffkit.providers.pge.statements import parse as parse_module
from tariffkit.providers.pge.statements.model import Section

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


def test_a_rate_glued_to_its_at_sign_does_not_bill_the_kwh_as_dollars() -> None:
    """Whether "@ $0.10867" is one field or two depends on how wide the gap printed.

    `_fields` splits on two or more spaces, and on the 2026-09-04 statement that
    gap came out a single space. Everything downstream looks for "@" as a field
    of its own, so the row fell through to the fallback scan -- which takes the
    first money-shaped field, and `MONEY` allows six decimals, so the *quantity*
    "10.122000" was read as a $10.12 charge. Three rows over, the delivery
    section summed to 51.67 against a printed 21.07.
    """
    line = " Off Peak                10.122000     kWh    @ $0.10867                  1.10"
    row = parse_module._line(line, Section.PGE_DELIVERY, page=0)
    assert row is not None
    assert row.amount == pytest.approx(1.10), "the charge, not the kilowatt-hours"
    assert row.quantity == pytest.approx(10.122)
    assert row.rate == pytest.approx(0.10867)


def test_one_period_printed_twice_is_one_agreement() -> None:
    """The day count is evidence about a span, not part of its identity.

    Deduplicating on (start, end, days) meant a period printed twice with two
    different day counts came back as two agreements, which `_agreements`
    refuses the whole statement over. On the recognised statements a misread
    digit is enough to produce exactly that.
    """
    page = (
        "09/29/2025 to 10/28/2025 (30 billing days)\n09/29/2025 to 10/28/2025 (3O billing days)\n"
    ).replace("3O", "31")
    spans = parse_module._agreement_spans(page)
    assert len(spans) == 1, f"one period, however many day counts: {spans}"
    assert (spans[0].start, spans[0].end) == (date(2025, 9, 29), date(2025, 10, 28))


def test_a_credit_balance_statement_has_no_total_and_is_not_an_error() -> None:
    """An account in credit is issued a statement with no "Total Amount Due".

    Nothing is due, so the utility prints "CREDIT BALANCE - NO PAYMENT DUE" and
    the negative balance instead. Refusing the statement for the absence of a
    line it is correct not to have reported "no total amount due found" -- true,
    and not an error -- and took the whole sync down with it.

    Layout extraction splits the label around its own figure, which is why the
    pattern spans lines: the amount lands between "NO PAYMENT" and "DUE".
    """
    pages = [
        "\n".join(
            [
                "Statement Date: 09/04/2026",
                "07/29/2026 to 08/27/2026 (30 billing days)",
                "Your Account Summary",
                "    Current PG&E Electric Monthly Charges          $21.07",
                "    Electric Adjustments                          -36.18",
                "    CREDIT BALANCE - NO PAYMENT",
                "                                                 -$21.96",
                "    DUE",
            ]
        )
    ]
    statement = parse_statement(pages)
    assert statement.amount_due == pytest.approx(-21.96)


def test_a_misread_date_is_a_statement_error_not_a_valueerror() -> None:
    """Recognition can turn a digit into an impossible date, and must survive it.

    The pre-November-2025 statements draw every character with a font whose
    ToUnicode map calls most glyphs spaces, so they are read by recognition,
    which can misread. `read_statement` knows that: its reading loop discards a
    reading that raises `StatementError` and tries the next one.

    A misread that produced `41/12/2026` escaped the loop entirely, because
    `date()` raises `ValueError` and the guard catches `StatementError`. One
    statement out of twenty-one in a real sync printed
    `ValueError: month must be in 1..12, not 41` as a traceback from inside the
    loop whose whole purpose is to survive exactly that.
    """
    pages = [
        "\n".join(
            [
                "Statement Date: 02/05/2026",
                "Your Account Summary",
                "Total Amount Due                                      10.00",
                "41/12/2026 to 12/31/2026 (30 billing days)",
            ]
        )
    ]
    with pytest.raises(StatementError, match="is not a date"):
        parse_statement(pages)


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
