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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

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

    reader_pages = list(PdfReader(str(path)).pages)
    pages: list[Page] = []
    for index, raw in enumerate(reader_pages):
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
    _require_text_layer(path, pages, reader_pages)
    return pages


#: Below this many characters a page is treated as having no text, whatever it
#: nominally extracted: "Page 1" is six.
MIN_TEXT_CHARS = 40


def _require_text_layer(path: Path, pages: list[Page], raw_pages: Sequence[Any]) -> None:
    """Fail with the actual reason when a document has no readable text.

    There are three ways to end up with a PDF nothing can read, and they need
    different answers, so guessing between them wastes the reader's time:

    * the download was blocked and an error page got saved -- retry, or use --pdf
    * the document is a scan -- OCR, or find another publication of it
    * the document was printed to PDF with a font carrying no Unicode mapping

    The third is the one that is easy to misdiagnose, because the file is large,
    structurally valid, and full of drawing operators. MCE's current rate card is
    exactly this: 1.4 MB of content streams whose ToUnicode CMap has six entries,
    enough to spell "Page 1" and nothing else. Every glyph in the rate table is a
    bare glyph id with no character behind it. Their 2023 card extracts
    perfectly, so this is a change in how they publish, not a gap here.

    **A document with no text layer is not a dead end.** It cannot be *parsed*,
    but the page renders, so it can be read -- which is how the current MCE
    values were obtained and re-verified. The distinction that matters is
    between "no text parser can reach this" and "no data", and only the first
    is true. A CI runner has no reader, so it detects the change by checksum and
    leaves the reading to a session that does; see
    :func:`nem_rates.regen.cca._watch_by_checksum`.
    """
    if sum(len(p.text.strip()) for p in pages) >= MIN_TEXT_CHARS:
        return

    content_bytes = 0
    mapped = 0
    for raw in raw_pages:
        try:
            contents = raw["/Contents"]
            streams = contents if isinstance(contents, list) else [contents]
            content_bytes += sum(len(s.get_object().get_data()) for s in streams)
            for font in raw["/Resources"]["/Font"].values():
                to_unicode = font.get_object().get("/ToUnicode")
                if to_unicode is not None:
                    mapped += to_unicode.get_object().get_data().count(b"<")
        except Exception:
            pass

    if content_bytes > 10_000:
        raise ExtractionError(
            f"{path} draws {content_bytes:,} bytes of content but exposes no readable "
            f"text (its fonts map {mapped} characters to Unicode). That is a "
            f"print-to-PDF export: the figures are glyph ids with no characters "
            f"behind them, so no text parser can reach them. The page still renders, "
            f"so read the table from it and update the vendored file directly -- that "
            f"is how the current MCE rate card was read, and it needs a reader rather "
            f"than a parser, not a person. Record when you read it in the file."
        )
    raise ExtractionError(
        f"{path} has no extractable text -- it may be a scan, or the download may "
        f"have been blocked and saved as an error page"
    )


def parse_effective(text: str) -> date | None:
    """The effective date a page states, in either published spelling.

    Returns ``None`` when what was matched is not a real date. Text extraction
    on a scanned or overlapping page produces things like "June 51, 2025", and a
    single such page in a 250-page filing must not take down a whole index --
    the page simply does not contribute a date.
    """
    if match := EFFECTIVE.search(text):
        try:
            return datetime.strptime(match.group(1), "%B %d, %Y").date()
        except ValueError:
            return None
    if match := EFFECTIVE_SHORT.search(text):
        month, day, year = (int(g) for g in match.groups())
        try:
            return date(year + 2000 if year < 100 else year, month, day)
        except ValueError:
            return None
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
