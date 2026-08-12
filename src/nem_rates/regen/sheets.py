"""Reading rate tables out of published PDFs.

Every rate document this package regenerates from -- PG&E tariff sheets, PG&E's
Net Billing Tariff, a CCA's residential rate card -- is a PDF of tables, and they
are far more regular than they look. A rate line is a label followed by one
figure per column, negatives in parentheses, and on PG&E sheets an optional
``(I)``/``(R)``/``(N)``/``(L)`` marker per cell saying the value increased,
reduced, is new, or is unchanged:

    Summer Usage                        $0.26299  $0.16388  $0.11878
    Nuclear Decommissioning (all usage) ($0.00002) ($0.00002) ($0.00002)

This module holds the parts every extractor needs: turning a PDF into pages that
know their own provenance, and pulling ``(label, values)`` out of a page's text.
What each table *means* belongs to the extractor for that dataset.

pypdf is imported lazily so the library keeps working without the ``regen``
extra. Nothing on the pricing path reaches this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ..errors import DataError

#: A dollar figure, optionally parenthesised for negative, optionally trailed by
#: a change marker. Also matches a bare decimal, which is how MCE writes kWh
#: quantities and PG&E writes baseline allowances.
CELL = re.compile(r"(\()?\$?\s?([0-9]+\.[0-9]+)\)?\s*(?:\([IRNL]\))?")
#: Everything after a label: a run of one or more cells, to end of line.
TRAILING_CELLS = re.compile(r"((?:\(?\$?\s?[0-9]+\.[0-9]+\)?\s*(?:\([IRNL]\))?\s*)+)$")

SHEET_HEADER = re.compile(r"ELECTRIC SCHEDULE\s+(\S+)\s+Sheet\s+(\d+)", re.I)
ADVICE = re.compile(r"Advice\s+(\d+-E)", re.I)
EFFECTIVE = re.compile(r"Effective\s+([A-Z][a-z]+ \d{1,2}, \d{4})")
#: How a CCA rate card dates itself, e.g. "(Rates effective 1.1.23)".
EFFECTIVE_SHORT = re.compile(r"[Rr]ates effective\s+(\d{1,2})\.(\d{1,2})\.(\d{2,4})")


class ExtractionError(DataError):
    """A published document did not match the structure we rely on."""


@dataclass(frozen=True, slots=True)
class Page:
    """One page, with whatever provenance it carries.

    Tariff books reissue pages independently, so an advice letter and effective
    date belong to the page rather than the document -- see
    :func:`nem_rates.regen.tariff.pick_effective` for why that distinction
    decides how a snapshot is dated.
    """

    index: int
    text: str
    sheet_number: int | None = None
    advice_letter: str | None = None
    effective: date | None = None


def read_pages(path: Path) -> list[Page]:
    """Every page of ``path``, with its provenance parsed out."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - exercised by packaging
        raise ExtractionError(
            "regenerating rate data needs the 'regen' extra: pip install 'nem-rates[regen]'"
        ) from exc

    pages: list[Page] = []
    for index, raw in enumerate(PdfReader(str(path)).pages):
        text = raw.extract_text() or ""
        head = SHEET_HEADER.search(text)
        advice = ADVICE.search(text)
        pages.append(
            Page(
                index=index,
                text=text,
                sheet_number=int(head.group(2)) if head else None,
                advice_letter=advice.group(1).upper() if advice else None,
                effective=parse_effective(text),
            )
        )
    if not pages:
        raise ExtractionError(f"{path} has no pages")
    if not any(p.text.strip() for p in pages):
        # A scanned or mis-fetched file extracts to nothing. Saying so beats
        # every downstream table reporting itself as missing.
        raise ExtractionError(
            f"{path} has no extractable text -- it may be a scanned image, or the "
            f"download may have been blocked and saved as an error page"
        )
    return pages


def parse_effective(text: str) -> date | None:
    """The effective date a page states, in either published spelling."""
    if match := EFFECTIVE.search(text):
        return datetime.strptime(match.group(1), "%B %d, %Y").date()
    if match := EFFECTIVE_SHORT.search(text):
        month, day, year = (int(g) for g in match.groups())
        return date(year + 2000 if year < 100 else year, month, day)
    return None


def cells(blob: str) -> list[float]:
    """Every figure in ``blob``, negative when parenthesised."""
    return [(-1.0 if m.group(1) else 1.0) * float(m.group(2)) for m in CELL.finditer(blob)]


def clean_label(raw: str) -> str:
    """Fold a label to its component name.

    Strips footnote markers and the "(all usage)" qualifier, so
    ``"Transmission* (all usage)"`` and ``"Transmission"`` are one key.
    """
    text = re.sub(r"\*+", " ", raw)
    text = re.sub(r"\((?:all|Bundled)\s+usage\)", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" :|").lower()


def rate_lines(text: str) -> list[tuple[str, list[float]]]:
    """``(label, values)`` for every line carrying figures.

    A label that wrapped onto its own line is joined to the values beneath it,
    which is how "Bundled Power Charge Indifference Adjustment / (all usage)***"
    appears on PG&E's EV2-A and E-ELEC sheets.
    """
    out: list[tuple[str, list[float]]] = []
    pending: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().rstrip("|").strip()
        if not line:
            continue
        match = TRAILING_CELLS.search(line)
        if not match:
            # Keep only the last few: a wrapped label is short, and holding more
            # would glue an unrelated paragraph onto the next figure.
            pending = [*pending, line][-3:]
            continue
        label = line[: match.start()].strip() or " ".join(pending)
        pending = []
        out.append((label, cells(match.group(1))))
    return out


def find_page(pages: list[Page], *needles: str) -> Page:
    """The first page containing every needle, or an error naming them."""
    for page in pages:
        if all(needle in page.text for needle in needles):
            return page
    raise ExtractionError(f"no page contains all of {needles!r}")


def season_of(label: str) -> str | None:
    """``"summer"``/``"winter"`` when a label names one, else ``None``."""
    low = label.strip().lower()
    if low.startswith("summer"):
        return "summer"
    if low.startswith("winter"):
        return "winter"
    return None
