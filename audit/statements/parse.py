"""Turning a statement PDF into a :class:`Statement`.

PG&E's statements carry a real text layer, so this is extraction rather than
recognition -- ``extract_text(extraction_mode="layout")`` preserves the column
positions, and a charge row is a label followed by two or more spaces and a
right-aligned amount. That is the whole trick.

Two things stop it being that simple, and both are handled by finding the column
a section starts in rather than by hoping:

* **Pages are multi-column.** The unbundled breakdown shares its rows with a
  column of safety notices and rate-assistance prose, so a naive "first field is
  the label" split reads the prose. Each section is therefore sliced to the
  column its own heading starts in.
* **Detail pages carry a right-hand sidebar** of service information and
  messages. Those never precede the amount, so taking the *first* money-shaped
  field after the label is enough to ignore them.

The split between :func:`read_statement` and :func:`parse_statement` is
load-bearing. Everything interesting happens in the second, which takes page text
and no file, so the whole parser is testable against synthetic fixtures and only
one thin function ever needs a PDF. Real statements carry a name, an address and
an account number, so none is committed; they are read where they already sit and
nothing derived from them is stored.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

from nem_rates.billing import BillingPeriod

from ..errors import StatementError
from .model import Section, Statement, StatementLine, StatementSection

#: A money field: "$333.87", "-1.96", "-$0.10084", "1,234.56".
MONEY = re.compile(r"^-?\$?-?\d[\d,]*\.\d{2,6}$")
#: A bare quantity, e.g. "982.126000".
QUANTITY = re.compile(r"^\d[\d,]*\.?\d*$")

#: Where each section begins. Ordered: the first match on a page opens it, and
#: the next heading anywhere closes it, so a section running onto the following
#: page keeps collecting rows without needing to know it did.
ANCHORS: tuple[tuple[Section, re.Pattern[str]], ...] = (
    (Section.SUMMARY, re.compile(r"Your Account Summary")),
    (Section.PGE_DELIVERY, re.compile(r"Details of PG&E Electric Delivery Charges")),
    (
        Section.CCA_GENERATION,
        re.compile(r"Details of (?P<cca>[A-Z][A-Za-z& ]+?) Electric Generation"),
    ),
    (Section.PGE_BREAKDOWN, re.compile(r"Your Electric Charges Breakdown")),
)

#: The row that ends a section by stating its total. Everything below it on the
#: page belongs to something else -- on the delivery detail, a monthly-history
#: chart whose bars are money-shaped and would otherwise be read as charges.
#:
#: The indent bound is doing real work. A section's own total sits hard against
#: its left edge, while the right-hand sidebar has a "Total Usage" line more than
#: a hundred columns in; without the bound that sidebar closes the section
#: two-thirds of the way through and the rest of the charges vanish.
TOTAL_ROW = re.compile(r"^ {0,10}Total\b", re.I)

#: A row whose label is printed on the line *below* it, marked with a lone dot in
#: the label column. PG&E uses it for charges that carry their own quantity and
#: rate, such as the Base Services Charge and the baseline allowance.
DEFERRED_LABEL = re.compile(r"^\s*\.\s")

#: How far past a wrapped "Total ..." label to keep looking for its amount.
#: Three is enough for the sidebar lines seen interleaved on the 2025 layout.
TOTAL_WRAP_LINES = 3

#: "12/30/2025 to 01/29/2026 (31 billing days)" -- the cycle.
#:
#: Both separators are real, and the older one is an *en dash*, not a hyphen:
#: statements through 2025 separate the dates with an en dash (U+2013) while
#: the redesign switched to "to". Matching only "to" rejects every older
#: statement with "no
#: billing cycle found", and matching a plain hyphen silently still misses them,
#: which reads as a broken PDF rather than as a layout nobody taught this parser.
CYCLE = re.compile(
    r"(\d{2}/\d{2}/\d{4})\s+(?:to|[-\u2013\u2014])\s+(\d{2}/\d{2}/\d{4})"
    r"(?:\s*\((\d+)\s+billing\s+days\))?"
)
#: A sub-period block heading inside the delivery detail: the same shape as the
#: cycle header, minus the day count, which is how the two are told apart.
SUBPERIOD = re.compile(
    r"^\s*(\d{2}/\d{2}/\d{4})\s+(?:to|[-\u2013\u2014])\s+(\d{2}/\d{2}/\d{4})(?:\s|$)"
)

STATEMENT_DATE = re.compile(r"Statement\s+Date:\s*(\d{2}/\d{2}/\d{4})")
ACCOUNT = re.compile(r"Account\s+N(?:o|umber)[.:]?\s*(\d[\d-]+)")
USAGE = re.compile(r"Electric\s+Usage\s+This\s+Period:\s*([\d,]+\.?\d*)\s*kWh,?\s*(\d+)\s+billing")
RATE_SCHEDULE = re.compile(r"Rate\s+Schedule:\s*(.+?)\s*$", re.M)
BASELINE_TERRITORY = re.compile(r"Baseline\s+Territory\s+([A-Z])\b")
PCIA_VINTAGE = re.compile(r"(\d{4})\s+Vintaged\s+Power\s+Charge")

#: Rows that are structure rather than charges.
SKIP_LABELS = re.compile(
    r"^(energy charges|baseline allowance|service for|rate schedule|account n|statement date|"
    r"due date|total )",
    re.I,
)


def _money(field: str) -> float | None:
    """A printed amount as a float, or None if this field is not one."""
    text = field.strip()
    if not MONEY.match(text):
        return None
    negative = text.startswith("-")
    return (-1.0 if negative else 1.0) * float(text.lstrip("-").lstrip("$").replace(",", ""))


def _fields(line: str) -> list[str]:
    """Split a laid-out row into its columns. Two or more spaces is a gap."""
    return [part for part in re.split(r"\s{2,}", line.strip()) if part]


def _parse_date(text: str) -> date:
    month, day, year = (int(part) for part in text.split("/"))
    return date(year, month, day)


def read_statement(path: str | Path) -> Statement:
    """Read a statement PDF.

    The only function here that touches a file or needs pypdf.
    """
    source = Path(path)
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - exercised by the extra's absence
        raise StatementError(
            "reading a statement PDF needs pypdf: pip install 'nem-rates[regen]'"
        ) from exc

    reader = PdfReader(source)
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text(extraction_mode="layout") or "")
        except KeyError:
            # A page with no content stream. Real: PG&E pads statements with a
            # trailing blank page, and pypdf raises rather than returning "".
            pages.append("")

    if not any(text.strip() for text in pages):
        raise StatementError(
            f"{source.name} has no text layer, so it is a scan or a print-to-PDF export "
            f"rather than the statement PG&E serves; download it again from the portal"
        )
    return parse_statement(pages, source=source.name)


def parse_statement(pages: Sequence[str], *, source: str = "") -> Statement:
    """Parse already-extracted page text. No file, no network, no clock."""
    joined = "\n".join(pages)

    stamp = STATEMENT_DATE.search(joined)
    if not stamp:
        raise StatementError(f"{source or 'statement'}: no statement date found")

    cycle = CYCLE.search(joined)
    if not cycle:
        raise StatementError(f"{source or 'statement'}: no billing cycle found")
    period = BillingPeriod(_parse_date(cycle.group(1)), _parse_date(cycle.group(2)))

    sections = _sections(pages)

    summary = next((s for s in sections if s.name is Section.SUMMARY), None)
    if summary is None or summary.printed_total is None:
        raise StatementError(f"{source or 'statement'}: no total amount due found")

    usage = USAGE.search(joined)
    account = ACCOUNT.search(joined)
    schedules = RATE_SCHEDULE.findall(joined)
    territory = BASELINE_TERRITORY.search(joined)
    vintage = PCIA_VINTAGE.search(joined)
    cca = ANCHORS[2][1].search(joined)

    return Statement(
        statement_date=_parse_date(stamp.group(1)),
        period=period,
        amount_due=summary.printed_total,
        account_masked=re.sub(r"\D", "", account.group(1))[-4:] if account else "",
        billed_days=int(cycle.group(3)) if cycle.group(3) else None,
        billed_kwh=float(usage.group(1).replace(",", "")) if usage else None,
        sections=sections,
        rate_schedule=schedules[0].strip() if schedules else "",
        cca_name=cca.group("cca").strip() if cca else "",
        cca_rate_schedule=schedules[1].strip() if len(schedules) > 1 else "",
        baseline_territory=territory.group(1) if territory else "",
        pcia_vintage=int(vintage.group(1)) if vintage else None,
        source=source,
    )


def _sections(pages: Sequence[str]) -> tuple[StatementSection, ...]:
    """Collect each section's rows, sliced to the column its heading starts in.

    A section runs from its heading to its total row. Closing on the total is
    what keeps the delivery detail's monthly-history chart out of the charges:
    those bars are money-shaped and sit directly beneath the total.
    """
    found: dict[Section, list[StatementLine]] = {}
    text: dict[Section, list[str]] = {}
    totals: dict[Section, float] = {}
    current: Section | None = None
    column = 0
    pending_total: Section | None = None
    pending_span = 0
    awaiting_label: StatementLine | None = None

    for index, page in enumerate(pages):
        for raw in page.splitlines():
            opened = _anchor(raw)
            if opened is not None:
                current, column = opened
                pending_total = None
                found.setdefault(current, [])
                text.setdefault(current, [])
                continue

            line = raw[column:] if column else raw

            # The total's label sometimes wraps, leaving the amount on the row
            # below: "Total MCE Electric Generation" / "Charges   $134.54".
            #
            # Not necessarily the *next* row, though. On the older layout the
            # right-hand sidebar interleaves its own lines between the two, so
            # giving up after one row loses the total and the statement then
            # fails its own checks with a whole section apparently missing.
            # Bounded, because scanning on indefinitely would eventually adopt
            # some unrelated amount as the section total.
            if pending_total is not None:
                amount = _first_money(line)
                if amount is not None:
                    totals[pending_total] = amount
                    pending_total = None
                    pending_span = 0
                    continue
                pending_span += 1
                if pending_span > TOTAL_WRAP_LINES:
                    pending_total = None
                    pending_span = 0
                continue

            if current is None:
                continue

            # A dot-marked row holds its amount but not its name; the next row
            # supplies the label and carries nothing of its own.
            if awaiting_label is not None:
                fields = _fields(line)
                label = fields[0].strip() if fields else ""
                if label:
                    found[current].append(replace(awaiting_label, label=label))
                awaiting_label = None
                continue

            if DEFERRED_LABEL.match(line):
                text[current].append(line)
                # Only a priced row is a charge. The same dot marker also
                # introduces the baseline allowance -- "281.30 kWh (29 days)" --
                # which states a quantity and no money; reading its kWh as
                # dollars adds a few hundred to the section and looks exactly
                # like a real overcharge.
                if "@" in _fields(line):
                    deferred = _line(line, current, index, label="?")
                    if deferred is not None:
                        awaiting_label = deferred
                continue

            if TOTAL_ROW.match(line):
                amount = _first_money(line)
                if amount is not None:
                    totals[current] = amount
                    current = None
                else:
                    pending_total, current = current, None
                    pending_span = 0
                continue

            text[current].append(line)
            parsed = _line(line, current, index)
            if parsed is not None:
                found[current].append(parsed)

    return tuple(
        StatementSection(
            name=name,
            lines=tuple(_with_subperiods(found[name], text[name], name)),
            printed_total=totals.get(name),
        )
        for name in Section
        if name in found
    )


def _first_money(line: str) -> float | None:
    for candidate in _fields(line):
        amount = _money(candidate)
        if amount is not None:
            return amount
    return None


def _anchor(raw: str) -> tuple[Section, int] | None:
    for name, pattern in ANCHORS:
        match = pattern.search(raw)
        if match:
            # Slice a little left of the heading: rows in the same column are
            # sometimes indented one or two characters further left than it.
            return name, max(0, match.start() - 2)
    return None


def _line(line: str, section: Section, page: int, *, label: str = "") -> StatementLine | None:
    """One row, if it carries an amount.

    A rate row reads "<quantity> <unit> @ <rate> <amount>". Anchoring on the "@"
    rather than on the word "kWh" is what lets the Base Services Charge -- billed
    "29 days @ $0.79343" -- come out as a charge instead of having its *rate*
    read as its amount, which is the shape of error that looks like a $22
    discrepancy.
    """
    fields = _fields(line)
    if len(fields) < 2:
        return None

    label = label or fields[0].strip(" .")
    if not label or SKIP_LABELS.match(label):
        return None

    quantity = rate = None
    unit = ""
    rest = fields[1:]
    if "@" in rest:
        at = rest.index("@")
        priced = rest[:at]
        if len(priced) >= 2 and QUANTITY.match(priced[-2]):
            quantity = float(priced[-2].replace(",", ""))
            unit = priced[-1]
        rest = rest[at + 1 :]
        if rest:
            rate = _money(rest[0])
            rest = rest[1:]

    for candidate in rest:
        amount = _money(candidate)
        if amount is not None:
            return StatementLine(
                label=label,
                amount=amount,
                section=section,
                page=page,
                quantity=quantity,
                unit=unit,
                rate=rate,
                raw=line.rstrip(),
            )
    return None


def _with_subperiods(
    lines: Sequence[StatementLine], text: Sequence[str], section: Section
) -> list[StatementLine]:
    """Tag rows with the rate-change block they appeared under.

    Only the delivery detail prints these. A cycle spanning a rate change gets
    one block per side, which is the statement independently confirming that
    effective-dated pricing is the right model rather than an over-engineering.
    """
    if section is not Section.PGE_DELIVERY:
        return list(lines)

    spans: list[tuple[int, tuple[date, date]]] = []
    for position, raw in enumerate(text):
        match = SUBPERIOD.match(raw)
        # The cycle header has the same shape; its day count is what tells them
        # apart, and treating it as a block would put every row in one span.
        if match and "billing day" not in raw.lower():
            spans.append((position, (_parse_date(match.group(1)), _parse_date(match.group(2)))))
    if len(spans) < 2:
        return list(lines)

    # Walk rows and blocks together in document order.
    tagged: list[StatementLine] = []
    cursor = 0
    for position, raw in enumerate(text):
        if cursor < len(lines) and lines[cursor].raw == raw.rstrip():
            span = next((s for p, s in reversed(spans) if p < position), None)
            tagged.append(replace(lines[cursor], subperiod=span))
            cursor += 1
    return tagged if len(tagged) == len(lines) else list(lines)
