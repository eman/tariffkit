"""Reading a statement.

The fixture is synthetic. A real statement carries a name, an address, an
account number and a year of consumption, so none is committed here; the layout
is what matters and it is reproduced exactly, including every quirk that broke
an earlier version of the parser:

* a charge whose label prints on the row *below* it, marked with a lone dot
* the Base Services Charge, billed "31 days @ $0.79343" rather than per kWh
* the baseline allowance, which uses the same dot marker but states a quantity
  and no money
* a right-hand sidebar containing the words "Total Usage"
* a section total whose label wraps, leaving the amount on the next row
* an unbundled breakdown laid out in a column beside unrelated prose
* two sub-period blocks, because the cycle spans a rate change

Every one of those produced a wrong total that looked like a plausible billing
discrepancy rather than a parse failure, which is the whole reason
:meth:`Statement.self_check` exists and gates reconciliation.

Real statements are read by :class:`TestRealStatements`, which finds them
wherever they already are and stores nothing. They are deliberately gitignored.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from audit.errors import StatementError
from audit.statements import Section, parse_statement, read_statement
from audit.statements.parse import _fields, _money

FIXTURES = Path(__file__).parent / "fixtures" / "statements"
SYNTHETIC = FIXTURES / "synthetic_cca_ratechange.txt"


def load(path: Path = SYNTHETIC) -> list[str]:
    return path.read_text(encoding="utf-8").split("\x0c")


@pytest.fixture
def statement():  # type: ignore[no-untyped-def]
    return parse_statement(load(), source=SYNTHETIC.name)


class TestMoney:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("$333.87", 333.87),
            ("-1.96", -1.96),
            ("-$0.10084", -0.10084),
            ("1,234.56", 1234.56),
            ("0.02", 0.02),
        ],
    )
    def test_printed_forms_parse(self, text: str, expected: float) -> None:
        assert _money(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["kWh", "@", "29", "days", "", "USCA-PGXX-0400"])
    def test_non_amounts_are_rejected(self, text: str) -> None:
        # "29" matters: it is the day count on the Base Services Charge row, and
        # reading it as dollars puts a plausible $29 into the bill.
        assert _money(text) is None

    def test_columns_split_on_two_or_more_spaces(self) -> None:
        assert _fields("Distribution        182.21") == ["Distribution", "182.21"]
        # A single space is inside a label, not between columns.
        assert _fields("Recovery Bond Credit    -7.73") == ["Recovery Bond Credit", "-7.73"]


class TestParsedStatement:
    def test_the_cycle_and_its_own_day_count(self, statement) -> None:  # type: ignore[no-untyped-def]
        assert (statement.period.start.isoformat(), statement.period.end.isoformat()) == (
            "2025-12-30",
            "2026-01-29",
        )
        assert statement.billed_days == 31 == statement.period.days

    def test_it_reports_what_it_says_about_itself(self, statement) -> None:  # type: ignore[no-untyped-def]
        # Used to catch a stale account configuration before it can produce a
        # confident, fabricated finding.
        assert statement.rate_schedule.startswith("Time-of-Use")
        assert statement.cca_name == "MCE"
        assert statement.cca_rate_schedule == "ETOUC"
        assert statement.baseline_territory == "X"
        assert statement.pcia_vintage == 2011

    def test_only_the_last_four_account_digits_survive(self, statement) -> None:  # type: ignore[no-untyped-def]
        assert len(statement.account_masked) == 4
        assert statement.account_masked.isdigit()

    def test_every_section_sums_to_its_printed_total(self, statement) -> None:  # type: ignore[no-untyped-def]
        for section in statement.sections:
            if section.name is Section.SUMMARY or section.printed_total is None:
                continue
            assert section.total() == pytest.approx(section.printed_total, abs=0.005)

    def test_the_two_views_of_the_same_money_agree(self, statement) -> None:  # type: ignore[no-untyped-def]
        # The utility prints its total twice: once as time-of-use lines, once
        # unbundled into components. Agreement between them is the strongest
        # evidence the parse is right, because they share no rows.
        delivery = statement.section(Section.PGE_DELIVERY)
        breakdown = statement.section(Section.PGE_BREAKDOWN)
        assert delivery.printed_total == pytest.approx(breakdown.printed_total)
        assert delivery.total() == pytest.approx(breakdown.total())

    def test_the_rate_change_splits_the_cycle(self, statement) -> None:  # type: ignore[no-untyped-def]
        # The statement splits itself at the rate change, which is the utility
        # independently confirming that effective-dated pricing is the right
        # model rather than an over-engineering.
        assert [f"{a}..{b}" for a, b in statement.subperiods] == [
            "2025-12-30..2025-12-31",
            "2026-01-01..2026-01-29",
        ]

    def test_a_per_day_charge_keeps_its_rate_not_its_rate_as_its_amount(self, statement) -> None:  # type: ignore[no-untyped-def]
        (charge,) = statement.section(Section.PGE_BREAKDOWN).find("Base Services Charge")
        assert charge.amount == pytest.approx(24.60)
        (metered,) = statement.section(Section.PGE_DELIVERY).find("Base Services Charge")
        assert (metered.quantity, metered.unit) == (31.0, "days")
        assert metered.rate == pytest.approx(0.79343)
        # Days are not energy, so this row contributes no kWh.
        assert metered.kwh is None

    def test_a_metered_row_keeps_quantity_and_rate(self, statement) -> None:  # type: ignore[no-untyped-def]
        (row,) = statement.section(Section.CCA_GENERATION).find("Off Peak Winter")
        assert (row.kwh, row.rate, row.amount) == pytest.approx((900.0, 0.135, 121.50))

    def test_the_allowance_is_not_read_as_money(self, statement) -> None:  # type: ignore[no-untyped-def]
        # "290.00 kWh (29 days)" states a quantity, not a charge. Reading it as
        # dollars adds a few hundred to the section and looks like an overcharge.
        assert not statement.section(Section.PGE_DELIVERY).find("Baseline Allowance")

    def test_a_subtotal_is_not_added_to_its_own_section(self, statement) -> None:  # type: ignore[no-untyped-def]
        # "Net Charges" restates the rows above it. Counting it doubles most of
        # the section, and the result is plausibly wrong rather than obviously.
        cca = statement.section(Section.CCA_GENERATION)
        assert [line.label for line in cca.lines if line.is_subtotal] == ["Net Charges"]
        assert cca.total() == pytest.approx(cca.printed_total)
        assert sum(line.amount for line in cca.lines) > cca.total()

    def test_the_state_surcharge_is_on_the_generation_page(self, statement) -> None:  # type: ignore[no-untyped-def]
        # On a CCA account it prints on the provider's page, not the utility's,
        # which is how it stayed unmodelled while every line on the utility's
        # pages reconciled to the cent.
        (tax,) = statement.section(Section.CCA_GENERATION).find("Energy Commission Tax")
        assert tax.amount == pytest.approx(0.30)

    def test_the_sidebar_does_not_close_a_section(self, statement) -> None:  # type: ignore[no-untyped-def]
        # A "Total Usage" line sits in the right-hand sidebar, more than a
        # hundred columns in, partway through the delivery detail. Treating it
        # as the section's total drops every row beneath it.
        delivery = statement.section(Section.PGE_DELIVERY)
        assert delivery.printed_total == pytest.approx(158.60)
        # The second sub-period's rows sit below that sidebar line, so their
        # survival is the evidence the section stayed open.
        assert len(delivery.find("Franchise Fee Surcharge")) == 2

    def test_the_breakdown_label_is_not_the_prose_beside_it(self, statement) -> None:  # type: ignore[no-untyped-def]
        breakdown = statement.section(Section.PGE_BREAKDOWN)
        labels = {line.label for line in breakdown.lines}
        assert "Distribution" in labels
        assert not any("PG&E offers" in label or "1-800" in label for label in labels)

    def test_a_clean_parse_reports_no_problems(self, statement) -> None:  # type: ignore[no-untyped-def]
        assert statement.self_check() == []


class TestSelfCheckCatchesMisparses:
    """Deliberately damaged input. Each mutation is a real failure mode."""

    def test_a_dropped_row_is_caught(self) -> None:
        # The exact failure the gate exists for: one row silently absent, so the
        # section is short by its amount and every remaining line still agrees.
        pages = [re.sub(r"^(.*)Distribution +80\.00$", "", page, flags=re.M) for page in load()]
        problems = parse_statement(pages).self_check()
        assert any("pge_breakdown" in problem for problem in problems)

    def test_a_missing_section_is_caught(self) -> None:
        # Drop the generation provider's detail page only. The summary keeps its
        # one-line reference to it, which is what makes the total still look
        # reachable and the loss easy to miss.
        pages = [page for page in load() if "Details of MCE Electric Generation" not in page]
        problems = parse_statement(pages).self_check()
        assert any("whole section is probably missing" in problem for problem in problems)

    def test_a_misread_cycle_is_caught(self) -> None:
        pages = [page.replace("(31 billing days)", "(45 billing days)") for page in load()]
        problems = parse_statement(pages).self_check()
        assert any("billing days" in problem for problem in problems)

    def test_the_two_views_disagreeing_is_caught(self) -> None:
        # Change one presentation's total and not the other's. Neither section
        # is internally inconsistent, so only the cross-check catches it.
        pages = [
            re.sub(r"(Total PG&E Electric Delivery Charges\s+\$)158\.60", r"\g<1>999.99", page)
            for page in load()
        ]
        problems = parse_statement(pages).self_check()
        assert any("two views" in problem for problem in problems)


class TestRefusesUnreadableInput:
    def test_no_statement_date_is_an_error(self) -> None:
        with pytest.raises(StatementError, match="no statement date"):
            parse_statement(["nothing resembling a statement"])

    def test_no_cycle_is_an_error(self) -> None:
        with pytest.raises(StatementError, match="no billing cycle"):
            parse_statement(["Statement Date: 02/05/2026"])


@pytest.mark.statements
class TestRealStatements:
    """Against actual statements, wherever they already are.

    Nothing is copied into the repository. Point ``NEM_RATES_STATEMENT_DIR`` at a
    directory of PDFs, or leave them in the gitignored download cache; without
    either, this skips.
    """

    @staticmethod
    def _pdfs() -> list[Path]:
        configured = os.environ.get("NEM_RATES_STATEMENT_DIR")
        root = Path(configured) if configured else Path(".cache/pge/statements")
        # Only PG&E's own naming. The directory may be somewhere general like a
        # desktop, and everything else in it is somebody else's PDF.
        return sorted(root.glob("PGE_*.pdf")) if root.is_dir() else []

    def test_each_one_parses_and_self_checks_clean(self) -> None:
        pdfs = self._pdfs()
        if not pdfs:
            pytest.skip("no statements available; set NEM_RATES_STATEMENT_DIR")
        for pdf in pdfs:
            statement = read_statement(pdf)
            assert statement.self_check() == [], f"{pdf.name}: {statement.self_check()}"
            assert statement.amount_due > 0
