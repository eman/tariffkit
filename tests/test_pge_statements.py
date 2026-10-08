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


def test_a_reading_that_parsed_is_reported_over_one_that_did_not() -> None:
    """The near-miss says more than a different reading's complaint.

    A reading that produced a whole Statement and came up short by a row is
    closer to right than one that could not read a word, and its problems name
    what to go and look at. Reporting the other one sent two investigations to
    the wrong page: a statement blamed "page 3 prints an unsupported tariff",
    from a reading whose "p.m." had vanished, while the reading that named the
    tariff correctly had failed its self-check for an unrelated reason.
    """
    unchecked = [["delivery: rows sum to 51.67 but the section prints 21.07"]]
    ambiguous = [StatementAmbiguityError("page 3 prints an unsupported tariff")]

    reported = parse_module._ocr_failure("probe.pdf", unchecked, ambiguous)
    assert "did not check out" in str(reported)
    assert "rows sum to 51.67" in str(reported), "the near-miss names the row"

    # With nothing that parsed, the ambiguity is still the best thing to say.
    assert parse_module._ocr_failure("probe.pdf", [], ambiguous) is ambiguous[0]
    # And with neither, the generic refusal stands.
    assert "did not produce" in str(parse_module._ocr_failure("probe.pdf", [], []))


def test_two_rows_sharing_a_truncated_label_are_not_an_overlap() -> None:
    """Recognition widens the gaps inside a label, and `_fields` splits on two.

    So "Current PG&E Electric Monthly Charges" and "Current Gas Charges" both
    come back labelled "Current" on a combined statement. The duplicate check
    keyed on the label alone and called that overlapping sections, refusing two
    real statements out of twenty-one. What it looks for is one row collected
    twice, and such a row carries the same amount both times.
    """
    pages = [
        "\n".join(
            [
                "Statement Date: 02/05/2026",
                "07/29/2026 to 08/27/2026 (30 billing days)",
                "Your Account Summary",
                "Current    PG&E Electric Monthly Charges          21.07",
                "Current    Gas Charges                            62.22",
                "Total Amount Due                                  83.29",
            ]
        )
    ]
    statement = parse_statement(pages)
    overlaps = [p for p in statement.self_check() if "appears twice" in p]
    assert overlaps == [], f"two different rows are not an overlap: {overlaps}"


def test_the_closest_reading_is_the_one_reported() -> None:
    """Fewest problems wins: that reading is nearest to being usable."""
    unchecked = [["a", "b", "c"], ["only one thing wrong"], ["d", "e"]]
    reported = str(parse_module._ocr_failure("probe.pdf", unchecked, []))
    assert "only one thing wrong" in reported
    assert "1 problem(s)" in reported


def test_a_dropped_hyphen_between_the_peak_hours_still_names_the_tariff() -> None:
    """Recognition loses the mark, the way it loses the "@" on a metered row.

    "Peak Pricing 4 - 9 p.m." comes back as "4 9 p.m.", no tariff is recognised,
    and `_agreements` refuses the whole statement as printing an unsupported
    one. Two statements in a run of twenty-one were lost to a single missing
    hyphen. Anchoring on the "p.m." after the hours is what makes the dash safe
    to drop -- a bare "49" is not a peak window.
    """
    assert normalize_tariff("Time-of-Use (Peak Pricing 4 9 p.m. Every Day)") == "E-TOU-C"
    assert normalize_tariff("Time-of-Use (Peak Pricing 5 8 p.m. Every Day)") == "E-TOU-D"
    assert normalize_tariff("G1 XB Residential Service") is None


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


def test_a_dot_marked_charge_with_its_rate_glued_to_the_at_sign_is_kept() -> None:
    """The same single-space gap, on a row whose label prints one line below.

    The dot marker introduces both the Base Services Charge, which is priced,
    and the baseline allowance, which is not, and the two were told apart by
    looking for "@" as a field of its own. On the 2026-10-05 statement the
    charge printed "@ $0.79343" with one space, the "@" stayed glued to the
    rate, and the row was taken for an allowance and dropped: the delivery
    section summed to 37.00 against a printed 62.39, short by the whole $25.39.
    """
    page = "\n".join(
        [
            " Details of PG&E Solar Billing Plan Charges",
            ".                    32   days   @ $0.79343               $25.39",
            "Base Services Charge",
            ".                    281.30 kWh (29 days)",
            "Baseline Allowance",
            "Solar Billing Plan Charges                                $25.39",
        ]
    )
    (section,) = parse_module._sections([page])
    assert [(row.label, row.amount) for row in section.lines] == [
        ("Base Services Charge", pytest.approx(25.39))
    ], "the priced row is a charge, the allowance still is not"
    assert section.printed_total == pytest.approx(25.39)


def test_a_credit_carried_from_the_last_statement_is_part_of_the_amount_due() -> None:
    """The 2026-10-05 statement opened on the credit the one before closed on.

    -21.96 carried, 62.39 delivery and -36.18 adjustments make the 4.25 it
    printed as due. The self-check knew only the last two, called the statement
    21.96 short with "a whole section probably missing", and refused it; and
    the electric figure reconciliation compares against came out 21.96 low.
    """
    pages = [
        "\n".join(
            [
                "Statement Date: 10/05/2026",
                "08/28/2026 to 09/28/2026 (32 billing days)",
                "Your Account Summary",
                "Credit Balance on Previous Statement          -$21.96",
                "Payment(s) Received Since Last Statement         0.00",
                "Outstanding Credit Balance                    -$21.96",
                "Current PG&E Electric Monthly Charges          $62.39",
                "Electric Adjustments                           -36.18",
                "Total Amount Due by 10/26/2026                  $4.25",
                " Details of PG&E Solar Billing Plan Charges",
                "Rate Schedule:  EELEC  Electric Home",
                "Off Peak    197.374000     kWh    @ $0.31580    62.39",
                "Solar Billing Plan Charges                     $62.39",
            ]
        )
    ]
    statement = parse_statement(pages)
    assert statement.carried_balance == pytest.approx(-21.96)
    assert statement.self_check() == []
    assert statement.electric_charges == pytest.approx(62.39), "this cycle's, not the account's"

    # A bill paid in full carries nothing, printed under the other name.
    paid = parse_statement(
        [
            pages[0]
            .replace("Credit Balance on Previous Statement          -$21.96", "")
            .replace("Outstanding Credit Balance                    -$21.96", "")
            .replace("Payment(s)", "Previous Unpaid Balance    0.00\nPayment(s)")
            .replace("$4.25", "$26.21")
        ]
    )
    assert paid.carried_balance == 0.0
    assert paid.self_check() == []


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


def test_a_cycle_split_at_a_rate_change_is_one_agreement() -> None:
    """The utility splits a cycle where a rate change lands, under one schedule.

    08/28-08/31 then 09/01-09/28, one Time-of-Use agreement, one meter. Read as
    two spans that is two agreements for one schedule, which `_agreements` calls
    ambiguous -- so a cycle crossing a rate change or the June 1 season boundary
    was refused whole rather than priced in the two blocks the tariff charges.
    Two real statements out of twenty-one were lost to this.
    """
    page = "08/28/2025 to 08/31/2025 (4 billing days)\n09/01/2025 to 09/28/2025 (28 billing days)\n"
    spans = parse_module._agreement_spans(page)
    assert len(spans) == 1, f"one agreement split at the change: {spans}"
    assert (spans[0].start, spans[0].end) == (date(2025, 8, 28), date(2025, 9, 28))


def test_spans_with_a_gap_between_them_stay_two_agreements() -> None:
    """The counterpart: joining must not paper over the case the check exists for.

    Two spans that do not continue one another are two agreements, and a page
    printing both under one schedule is genuinely ambiguous evidence.
    """
    page = (
        "08/01/2025 to 08/10/2025 (10 billing days)\n09/01/2025 to 09/28/2025 (28 billing days)\n"
    )
    assert len(parse_module._agreement_spans(page)) == 2


def test_the_gas_half_of_a_climate_credit_is_read() -> None:
    """PG&E credits gas and electricity separately, in April and October.

    Both halves print in the summary. The electric one was read by name and the
    gas one by nothing, so a combined April statement failed its own check by
    exactly that credit: 135.21 electric, -58.23 electric adjustments, 65.71
    generation, 62.22 gas and -67.03 unread, against a printed 137.88 that the
    five of them reach precisely.

    The label is matched on its opening word because recognition truncates a
    label at any wide gap inside it -- on the statement this came from it
    arrived as bare "Gas".
    """
    pages = [
        "\n".join(
            [
                "Statement Date: 05/05/2025",
                "03/31/2025 to 04/28/2025 (29 billing days)",
                "Your Account Summary",
                "    Current PG&E Electric Monthly Charges         135.21",
                "    Electric Adjustments                          -58.23",
                "    MCE Electric Generation Charges                65.71",
                "    Current Gas Charges                            62.22",
                "    Gas    Adjustments                            -67.03",
                "Total Amount Due                                  137.88",
            ]
        )
    ]
    statement = parse_statement(pages)
    assert statement.gas_adjustments == pytest.approx(-67.03)
    assert statement.amount_due == pytest.approx(137.88)


def test_the_gas_charges_row_is_not_mistaken_for_the_adjustment() -> None:
    """ "Current Gas Charges" is already read from the gas section's own total.

    Matching any label starting "gas" would take it instead, and the identity
    would then be wrong in the other direction.
    """
    pages = [
        "\n".join(
            [
                "Statement Date: 05/05/2025",
                "03/31/2025 to 04/28/2025 (29 billing days)",
                "Your Account Summary",
                "    Current Gas Charges                            62.22",
                "Total Amount Due                                   62.22",
            ]
        )
    ]
    assert parse_statement(pages).gas_adjustments is None


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


def _plan_statement(*, pge_bank: str, mce_closing_export: str = "114.15") -> list[str]:
    """The pages of a Solar Billing Plan statement that carry its banks.

    Figures are the 2026-10-05 statement's.
    """
    return [
        "\n".join(
            [
                "Statement Date: 10/05/2026",
                "08/28/2026 to 09/28/2026 (32 billing days)",
                "Your Account Summary",
                "Credit Balance on Previous Statement          -$21.96",
                "Outstanding Credit Balance                    -$21.96",
                "Current PG&E Electric Monthly Charges          $62.39",
                "Electric Adjustments                           -36.18",
                "MCE Electric Generation Charges                  0.00",
                "Total Amount Due by 10/26/2026                  $4.25",
            ]
        ),
        "\n".join(
            [
                " Details of PG&E Solar Billing Plan Charges",
                "08/28/2026 to 09/28/2026 (32 billing days)",
                "Rate Schedule:  EELEC  Electric Home       Imports      199.769000 kWh",
                "                                           Exports     -117.990000 kWh",
                "Off Peak    197.374000     kWh    @ $0.31580    62.39",
                "Solar Billing Plan Charges                     $62.39",
            ]
        ),
        "\n".join(
            [
                "YOUR ENERGY EXPORT CREDIT BANK",
                "                       Energy Delivered    Bonus Credits    Total Credits",
                pge_bank,
                "Earned This Bill (-118 kWh)    $2.13      $1.04      $3.17",
            ]
        ),
        "\n".join(
            [
                " Details of MCE Electric Generation Charges",
                "08/28/2026 to 09/28/2026 (32 billing days)",
                "Off Peak Summer     197.374000     kWh   @  $0.11878     18.70",
                "Energy Export Credits Applied            @  prices vary     -18.70",
                " Energy Export Bonus Credits Applied        @  $0.00000      0.00",
                "   Solar Export Credits (EEC) earned this cycle $40.23",
                "   Solar Export Bonus Credits (EEBC) earned this cycle $1.04",
                f"   Current Energy Export Credit (EEC) Balance ${mce_closing_export}",
                "   Current Energy Export Bonus Credit (EEBC) Balance $7.21",
                "Total MCE Electric Generation Charges          $0.00",
                "  YOUR ENERGY EXPORT CREDIT BANK",
                "  Beginning Balance                $98.79      MCE's generation charges",
            ]
        ),
    ]


def test_the_bank_a_cycle_opened_with_is_read_from_the_statement() -> None:
    """PG&E's from its table; MCE's worked back from what it closed on.

    MCE prints only a total opening, 98.79, so its two credits are each
    closing - earned + spent: 114.15 - 40.23 + 18.70 and 7.21 - 1.04 + 0.00.
    That they come to the printed total is the check on the arithmetic.
    """
    statement = parse_statement(
        _plan_statement(pge_bank="Beginning Balance       $11.96      $0.00      $11.96")
    )
    assert statement.bank_problems == ()
    bank = statement.opening_bank
    assert bank is not None
    assert (bank.delivery, bank.bonus) == (pytest.approx(11.96), 0.0)
    assert (bank.generation, bank.cca_bonus) == (pytest.approx(92.62), pytest.approx(6.17))


def test_a_printed_bank_that_cannot_be_read_is_a_problem_not_an_empty_bank() -> None:
    """An empty bank is the defect the opening exists to prevent."""
    columns = parse_statement(
        _plan_statement(pge_bank="Beginning Balance  $1.00  $11.96  $0.00  $12.96")
    )
    assert columns.opening_bank is None
    assert any("could not be read" in p for p in columns.self_check())

    arithmetic = parse_statement(
        _plan_statement(
            pge_bank="Beginning Balance       $11.96      $0.00      $11.96",
            mce_closing_export="115.15",
        )
    )
    assert arithmetic.opening_bank is None
    assert any("works back to 99.79" in p for p in arithmetic.self_check())

    # Neither of the CCA's balances found at all is the same failure, not a
    # CCA bank of zero.
    pages = _plan_statement(pge_bank="Beginning Balance       $11.96      $0.00      $11.96")
    pages[3] = pages[3].replace("Current Energy Export", "Present Energy Export")
    unread = parse_statement(pages)
    assert unread.opening_bank is None
    assert any("closing export, closing bonus" in p for p in unread.self_check())


def test_a_statement_before_pto_prints_no_bank() -> None:
    assert parse_statement(load_fixture()).opening_bank is None


def test_the_plan_bills_imports_and_exports() -> None:
    """It prints no "Electric Usage This Period", so the kWh came out as None.

    And with it went the audit's check of the meter against the statement,
    skipped without a word on every cycle after PTO.
    """
    statement = parse_statement(
        _plan_statement(pge_bank="Beginning Balance       $11.96      $0.00      $11.96")
    )
    assert statement.billed_kwh == pytest.approx(199.769)
    assert statement.billed_export_kwh == pytest.approx(117.990)
