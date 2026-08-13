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
from datetime import date, timedelta
from pathlib import Path

from nem_rates.billing import BillingPeriod

from ..errors import StatementError
from .model import Section, Statement, StatementLine, StatementSection

#: A money field: "$333.87", "-1.96", "-$0.10084", "1,234.56".
MONEY = re.compile(r"^-?\$?-?\d[\d,]*\.\d{2,6}$")
#: A bare quantity, e.g. "982.126000".
QUANTITY = re.compile(r"^\d[\d,]*\.?\d*$")
#: A quantity and its unit in one field, e.g. "138.984000 kWh". Whether these
#: arrive as one field or two depends on how wide the printed gap between them
#: came out, and on a recognised page that is not predictable.
METERED_FIELD = re.compile(r"^\d[\d,]*\.?\d*\s+(kwh|days)$", re.I)

#: Where each section begins. Ordered: the first match on a page opens it, and
#: the next heading anywhere closes it, so a section running onto the following
#: page keeps collecting rows without needing to know it did.
#: Whitespace between words is written ``\s+`` throughout, never a literal
#: space. Recognised pages space words by pixel geometry, so "Your Account
#: Summary" comes back as "Your   Account    Summary" -- and an anchor that
#: assumes single spaces silently matches nothing on every recognised
#: statement, which presents as "no total amount due found".
ANCHORS: tuple[tuple[Section, re.Pattern[str]], ...] = (
    (Section.SUMMARY, re.compile(r"Your\s+Account\s+Summary")),
    # Two headings, one section. Once solar is interconnected the account moves
    # to the Solar Billing Plan and the utility retitles this page -- so a
    # parser that knows only the first heading silently finds no delivery
    # detail at all on every post-PTO statement, and reports the whole section
    # as missing rather than as renamed.
    (
        Section.PGE_DELIVERY,
        re.compile(
            r"Details\s+of\s+PG&E\s+(?:Electric\s+Delivery|Solar\s+Billing\s+Plan)\s+Charges"
        ),
    ),
    (
        Section.CCA_GENERATION,
        re.compile(r"Details\s+of\s+(?P<cca>[A-Z][A-Za-z& ]+?)\s+Electric\s+Generation"),
    ),
    (Section.PGE_BREAKDOWN, re.compile(r"Your\s+Electric\s+Charges\s+Breakdown")),
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
#: statement with "no billing cycle found", and matching a plain hyphen still
#: misses them, which reads as a broken PDF rather than as a layout nobody
#: taught this parser.
CYCLE = re.compile(
    r"(\d{2}/\d{2}/\d{4})\s+(?:to|[-\u2013\u2014])\s+(\d{2}/\d{2}/\d{4})"
    r"(?:\s*\((\d+)\s+billing\s+days\))?"
)
#: A sub-period block heading inside the delivery detail: the same shape as the
#: cycle header, minus the day count, which is how the two are told apart.
SUBPERIOD = re.compile(
    r"^\s*(\d{2}/\d{2}/\d{4})\s+(?:to|[-\u2013\u2014])\s+(\d{2}/\d{2}/\d{4})(?:\s|$)"
)

#: Sub-headings that qualify the rows beneath them. They carry no amount of
#: their own, which is how they are told apart from charges.
#: Not anchored at the end: the right-hand sidebar bleeds a stray character
#: onto these rows, so requiring the line to contain nothing else never matches.
#: Carrying no amount is the real test, and it is applied separately.
BLOCK_HEADING = re.compile(
    r"^\s*(Energy Produced|Energy Delivered|Other Charges, Credits and Taxes)\b"
)

#: What the Solar Billing Plan calls its section total. It does not begin with
#: "Total", so the usual row never matches and the section is left with no
#: printed total to check its rows against.
SBP_TOTAL = re.compile(r"^\s*Solar\s+Billing\s+Plan\s+Charges\s")

#: Gas, on a combined statement. Taken from the gas section's own total rather
#: than from the summary: the summary prints "Current Gas Charges" with the
#: amount in a column that extraction drops entirely, so the only place the
#: number survives is the detail page.
GAS_TOTAL = re.compile(r"Total\s+Gas\s+Charges\s+\$?(-?[\d,]+\.\d{2})")

#: How many tariffs priced this statement, counted by delivery detail pages
#: rather than by "Service Agreement ID:" rows -- every statement carries at
#: least two of those, one for PG&E and one for the CCA, so counting them calls
#: an ordinary CCA statement a split one. A second *delivery* page is the thing
#: that only happens when the account changed tariff mid-cycle.
DELIVERY_PAGE = ANCHORS[1][1]

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

    if _glyphs_are_spaces(reader):
        # PG&E's pre-November-2025 statements draw every character with a Type 3
        # font whose ToUnicode map calls most glyphs spaces, so extraction
        # returns exactly that and no reader does better. The pixels are the
        # only honest source left. Recognition can misread a digit into a
        # plausible amount, which is why `self_check` gates reconciliation: a
        # statement prints its totals twice and a misread almost always breaks
        # the arithmetic between them.
        from .ocr import readings

        # Keep the first reading that survives the statement's own checks. The
        # criterion is the document's, not a preference: a bill prints its
        # totals twice and its sections have to sum to them, so a reading that
        # satisfies that has almost certainly read the figures correctly, and
        # one that does not has demonstrably not. Falling back to the first
        # reading keeps the failure reportable rather than raising, and it is
        # still gated -- `self_check` runs again before anything is priced.
        scored: list[tuple[int, int, Statement]] = []
        for pages in readings(source):
            candidate = parse_statement(pages, source=source.name, recognised=True)
            # Two criteria, in order. Adding up is the document's own test and
            # comes first. Among readings that pass it, prefer the one whose
            # labels are words: a row recognised as "s" carries its amount
            # correctly and still cannot be matched to anything, so the section
            # totals while a charge goes unclaimed.
            scored.append((0 if candidate.self_check() else 1, _legible(candidate), candidate))
        if not scored:
            raise StatementError(f"{source.name} produced no readable pages")
        scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return scored[0][2]
    return parse_statement(pages, source=source.name)


#: Above this share of glyphs mapping to U+0020, the document is not text.
#: Measured: PG&E's 2025 Type 3 statements sit at ~56%, the readable ones at 3%.
SPACE_GLYPH_SHARE = 0.25

_BFCHAR = re.compile(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]{4})>")


def _glyphs_are_spaces(reader: object) -> bool:
    """Whether the fonts claim most of their glyphs are spaces.

    Distinguishes "this statement is in a layout nobody taught the parser" from
    "this statement carries no readable text at all", which need completely
    different responses -- the first is a parser gap, the second is not fixable
    here at any effort.
    """
    total = blank = 0
    for page in reader.pages:  # type: ignore[attr-defined]
        resources = page.get("/Resources") or {}
        for ref in (resources.get("/Font") or {}).values():
            font = ref.get_object()
            if "/ToUnicode" not in font:
                continue
            try:
                cmap = font["/ToUnicode"].get_data().decode("latin-1", "replace")
            except Exception:
                # An unreadable CMap proves nothing either way, so it should
                # not count toward the verdict.
                continue
            for _, unicode_point in _BFCHAR.findall(cmap):
                total += 1
                blank += unicode_point.lower() == "0020"
    return total > 0 and blank / total > SPACE_GLYPH_SHARE


def _delivery_pages(pages: Sequence[str]) -> int:
    """How many delivery detail pages the statement opens.

    Counted as headings, not as occurrences. The same words appear mid-sentence
    in the statement's own prose -- "See the 'Generation Credit' on the 'Details
    of PG&E Electric Delivery Charges' page" -- and counting that made an
    ordinary single-agreement statement look like a split one, which silently
    suppressed its meter cross-check. A heading begins its line; a
    cross-reference does not.
    """
    return sum(
        1 for page in pages for line in page.splitlines() if DELIVERY_PAGE.match(line.lstrip())
    )


def _scalar(text: str, pattern: re.Pattern[str]) -> float | None:
    found = pattern.search(text)
    return _money(found.group(1)) if found else None


def _summary_amount(summary: StatementSection, label: str) -> float | None:
    """One named line out of the running balance, or None if absent."""
    matches = summary.find(label)
    return matches[0].amount if matches else None


def _legible(statement: Statement) -> int:
    """How many of a statement's charge rows carry a label a rule can claim.

    A count rather than a ratio, so a reading that finds more rows is not
    penalised for it. Used only to choose between recognitions of the same
    document, never to judge whether one is right.
    """
    from .mapping import rule_for

    return sum(
        1
        for section in statement.sections
        for line in section.charged
        if rule_for(section.name, line.label) is not None
    )


def parse_statement(
    pages: Sequence[str], *, source: str = "", recognised: bool = False
) -> Statement:
    """Parse already-extracted page text. No file, no network, no clock."""
    joined = "\n".join(pages)

    stamp = STATEMENT_DATE.search(joined)
    if not stamp:
        raise StatementError(f"{source or 'statement'}: no statement date found")

    # Chained, not unioned. A statement prints more than one dated header and
    # they are not all the electric cycle: a combined bill carries the gas cycle
    # too, offset by a day (09/29-10/28 electric against 09/30-10/29 gas), and a
    # statement covering two service agreements carries one header each
    # (06/01-06/02 then 06/03-06/29). Taking the first header calls a 29-day
    # cycle a 2-day one; taking the union swallows the gas cycle and calls a
    # 30-day cycle 60 days. Following only headers that continue the previous
    # one -- starting the day after it ended -- separates the two cases without
    # needing to know which service each belongs to.
    spans = sorted(
        {(_parse_date(start), _parse_date(end), days) for start, end, days in CYCLE.findall(joined)}
    )
    if not spans:
        raise StatementError(f"{source or 'statement'}: no billing cycle found")

    dated = [s for s in spans if s[2]] or spans
    chain = [dated[0]]
    for span in dated[1:]:
        if span[0] == chain[-1][1] + timedelta(days=1):
            chain.append(span)
    period = BillingPeriod(chain[0][0], chain[-1][1])
    billed_days = sum(int(s[2]) for s in chain if s[2]) or None

    sections = _sections(pages)

    summary = next((s for s in sections if s.name is Section.SUMMARY), None)
    if summary is None or summary.printed_total is None:
        raise StatementError(f"{source or 'statement'}: no total amount due found")

    # Summed over distinct blocks, not the first found. A statement covering
    # two service agreements states usage once per agreement -- 2 days then 27
    # -- and taking the first reports the cycle as having used a fraction of
    # what it did, which surfaces as the meter and the statement disagreeing by
    # hundreds of kilowatt-hours. Deduplicated because the utility's page and
    # the CCA's page each print the same figures.
    # Keyed on the parsed value, not the printed text: the same figure appears
    # as "621.046" in one place and "621.046000" in another, and deduplicating
    # the strings keeps both and doubles the cycle's usage.
    usage_blocks = {
        (round(float(kwh.replace(",", "")), 3), int(days)) for kwh, days in USAGE.findall(joined)
    }
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
        billed_days=billed_days,
        billed_kwh=(sum(kwh for kwh, _ in usage_blocks) or None if usage_blocks else None),
        service_agreements=max(1, _delivery_pages(pages)),
        gas_charges=_scalar(joined, GAS_TOTAL),
        electric_adjustments=_summary_amount(summary, "Electric Adjustments"),
        sections=sections,
        rate_schedule=schedules[0].strip() if schedules else "",
        printed_schedules=tuple(dict.fromkeys(s.strip() for s in schedules if s.strip())),
        cca_name=cca.group("cca").strip() if cca else "",
        cca_rate_schedule=schedules[1].strip() if len(schedules) > 1 else "",
        baseline_territory=territory.group(1) if territory else "",
        pcia_vintage=int(vintage.group(1)) if vintage else None,
        source=source,
        recognised=recognised,
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
    #: How many times each section's heading has opened. A statement covering
    #: two service agreements prints the delivery detail and the generation page
    #: once per agreement, with identical labels in each; without this they look
    #: like one section whose boundaries overlap.
    opened_count: dict[Section, int] = {}
    agreement = 0
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
                opened_count[current] = opened_count.get(current, 0) + 1
                agreement = opened_count[current]
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
                    totals[pending_total] = totals.get(pending_total, 0.0) + amount
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
                    found[current].append(replace(awaiting_label, label=label, agreement=agreement))
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

            if TOTAL_ROW.match(line) or SBP_TOTAL.match(line):
                amount = _first_money(line)
                if amount is not None:
                    # Added, not replaced. Each service agreement prints its own
                    # total, and together they are the cycle's -- 3.59 + 21.89
                    # on 2026-07-07. Overwriting keeps only the last and reports
                    # the section as short by the whole of the first.
                    totals[current] = totals.get(current, 0.0) + amount
                    current = None
                else:
                    pending_total, current = current, None
                    pending_span = 0
                continue

            text[current].append(line)
            parsed = _line(line, current, index)
            if parsed is not None:
                found[current].append(replace(parsed, agreement=agreement))

    return tuple(
        StatementSection(
            name=name,
            lines=tuple(_with_blocks(_with_subperiods(found[name], text[name], name), text[name])),
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


def _implied_at(rest: list[str]) -> int | None:
    """Where the "@" would be on a metered row that lost it.

    Recognised statements drop the symbol -- it is small, and it sits in a gap.
    The shape is still unmistakable: a quantity, its unit, then two amounts,
    the rate and the charge. Without this the rate is read as the charge, so a
    $86.96 line prints as $0.63 and the section quietly comes up short.

    Returns the index the rate starts at, or None when the row is not that
    shape. Deliberately strict: guessing here invents a charge.
    """
    for index, field in enumerate(rest):
        # The quantity and its unit may be one field or two, depending on how
        # wide the gap between them came out.
        metered = bool(METERED_FIELD.match(field)) or (
            index and QUANTITY.match(rest[index - 1]) and field.lower() in {"kwh", "days"}
        )
        if not metered:
            continue
        after = rest[index + 1 :]
        if len([value for value in after if _money(value) is not None]) >= 2:
            return index + 1
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

    derived = not label
    label = label or fields[0].strip(" .")
    # A section is sliced at the column its heading starts in, and on a
    # recognised page that column is an estimate. One character out to the left
    # and the tail of the neighbouring prose column arrives as the first field:
    # "...income-qualified households" becomes a label of "s", and the row's
    # real label sits in the next field. No charge on a statement is named by
    # one or two characters, so a fragment that short is dropped rather than
    # believed -- the amount is read correctly either way, and the cost of
    # believing it is a charge that reconciles against nothing.
    if derived and len(label) <= 2 and not label.isdigit() and len(fields) >= 3:
        fields = fields[1:]
        label = fields[0].strip(" .")
    if not label or SKIP_LABELS.match(label):
        return None

    quantity = rate = None
    unit = ""
    rest = fields[1:]
    at = rest.index("@") if "@" in rest else _implied_at(rest)
    if at is not None:
        priced = rest[:at]
        # The quantity and its unit may be one field or two, the same way the
        # "@" itself may or may not have survived. Reading only the two-field
        # form leaves a recognised row with no quantity at all, which silently
        # costs the reconciler its ability to re-price that row from the
        # statement's own metered figures.
        if priced and METERED_FIELD.match(priced[-1]):
            # split(), not partition(" "): the pattern allows any whitespace, so
            # a tab-separated "40.00\tkWh" matches and then partitions on a
            # space that is not there, and float() raises out of the parse.
            amount_text, unit = priced[-1].split(maxsplit=1)
            quantity = float(amount_text.replace(",", ""))
            unit = unit.strip()
        elif len(priced) >= 2 and QUANTITY.match(priced[-2]):
            quantity = float(priced[-2].replace(",", ""))
            unit = priced[-1]
        # The rate sits immediately after the "@", whether or not the symbol
        # itself survived. Skipping past it is the whole point: read as the
        # amount, $0.62569 becomes a 63-cent charge where $86.96 was due.
        rest = rest[at:] if "@" not in rest else rest[at + 1 :]
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


def _with_blocks(lines: Sequence[StatementLine], text: Sequence[str]) -> list[StatementLine]:
    """Tag rows with the sub-heading they sit under.

    Needed once solar is interconnected: the Solar Billing Plan prints the same
    three time-of-use labels twice, under "Energy Produced" and under "Energy
    Delivered". They are export and import, and telling them apart is the
    difference between a credit and a charge.
    """
    heads: list[tuple[int, str]] = []
    for index, raw in enumerate(text):
        head = BLOCK_HEADING.match(raw)
        if head and _first_money(raw) is None:
            heads.append((index, head.group(1).strip()))
    if not heads:
        return list(lines)

    tagged: list[StatementLine] = []
    cursor = 0
    for position, raw in enumerate(text):
        if cursor < len(lines) and lines[cursor].raw == raw.rstrip():
            under = next((h for p, h in reversed(heads) if p < position), "")
            tagged.append(replace(lines[cursor], block=under))
            cursor += 1
    return tagged if len(tagged) == len(lines) else list(lines)


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
