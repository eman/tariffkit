"""Reading a statement that carries no usable text.

PG&E's statements before November 2025 are drawn with Type 3 fonts whose
``ToUnicode`` map declares most glyphs to be U+0020. Extraction is not failing on
them -- it is faithfully returning the spaces the document asks for -- so no
choice of PDF library helps, and pypdf and poppler agree. The pixels are the only
honest source, which means rendering and recognising them.

The parser downstream expects ``extraction_mode="layout"`` text, where a row is a
label, two or more spaces, and a right-aligned amount. So this does not just
return words: it rebuilds the page as a character grid from the word boxes
tesseract reports, which is what makes the same parser work on both kinds of
statement rather than needing a second one.

**Recognition can be wrong, and that matters more here than usual** -- a misread
digit is a plausible dollar amount rather than an obvious failure. Nothing here
tries to be clever about that. It relies instead on the check that already
exists: a statement prints its totals twice, and :meth:`Statement.self_check`
refuses to reconcile one whose sections do not sum to what it says is due. A
digit recognised wrongly almost always breaks that arithmetic.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..errors import StatementError

#: Rendering resolution. 300 is not a floor to be raised for better results:
#: 400 was measurably worse here, losing the statement date entirely, because
#: tesseract's model is trained around this scale and the reconstructed grid
#: depends on its word boxes being consistent. Change it only with a statement
#: in front of you.
DPI = 300

#: Tesseract's page segmentation mode. 11 is "sparse text": find words, attempt
#: no layout analysis at all.
#:
#: That sounds like the wrong choice and is the right one, because the rows are
#: reassembled here from word geometry anyway. Tesseract's own analysis fights
#: that -- on a statement laid out as a table beside a sidebar, mode 6 silently
#: drops whole rows. It lost the Peak charge from the 2025-09 delivery table,
#: $104.43 of a $178.64 section, while the chart legend two inches lower
#: recognised the same figure perfectly. Mode 11 reads that row completely.
PSM = "11"

#: How much of a line's height two words may differ by and still be the same
#: row. Sparse mode does no line grouping, so rows are clustered by vertical
#: position here; a fraction rather than a constant because it has to hold at
#: whatever resolution the page was rendered at.
ROW_TOLERANCE = 0.6


def available() -> bool:
    """Whether the external tools needed for recognition are installed."""
    return bool(shutil.which("pdftoppm") and shutil.which("tesseract"))


def _render(source: Path, into: Path) -> list[Path]:
    result = subprocess.run(
        ["pdftoppm", "-r", str(DPI), "-png", str(source), str(into / "page")],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise StatementError(
            f"could not render {source.name} for recognition: "
            f"{result.stderr.decode('utf-8', 'replace')[:200]}"
        )
    return sorted(into.glob("page*.png"))


def _words(image: Path) -> list[tuple[int, int, int, int, str]]:
    """Recognised words as (top, height, left, width, text)."""
    result = subprocess.run(
        ["tesseract", str(image), "stdout", "--psm", PSM, "tsv"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise StatementError(
            f"recognition failed on {image.name}: {result.stderr.decode('utf-8', 'replace')[:200]}"
        )

    rows: list[tuple[int, int, int, int, str]] = []
    reader = csv.DictReader(result.stdout.decode("utf-8", "replace").splitlines(), delimiter="\t")
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            # Geometry only. Sparse mode reports no meaningful block, paragraph
            # or line numbering, and relying on any of it is what reordered rows
            # across sections before.
            rows.append(
                (
                    int(row["top"]),
                    int(row["height"]),
                    int(row["left"]),
                    int(row["width"]),
                    text,
                )
            )
        except (KeyError, ValueError):
            continue
    return rows


def _as_layout(words: list[tuple[int, int, int, int, str]]) -> str:
    """Rebuild a character grid from word boxes.

    The parser reads column positions, not just words, so the horizontal
    geometry has to survive. Each word is placed at the column its pixel offset
    implies, using the median character width as the scale -- which is what makes
    "Distribution" and its right-aligned amount stay in different fields.
    """
    if not words:
        return ""

    widths = sorted(width / len(text) for _, _, _, width, text in words if text)
    char_width = widths[len(widths) // 2] or 1.0
    heights = sorted(height for _, height, _, _, _ in words)
    band = (heights[len(heights) // 2] or 1) * ROW_TOLERANCE

    # Clustered down the page. Ordering by anything tesseract numbers rather
    # than by position reorders rows across section boundaries, and the symptom
    # is one section short by a charge while another is over by the same rows.
    grouped: list[list[tuple[int, str]]] = []
    anchor = None
    for top, _, left, _, text in sorted(words, key=lambda w: (w[0], w[2])):
        if anchor is None or top - anchor > band:
            grouped.append([])
            anchor = top
        grouped[-1].append((round(left / char_width), text))

    out: list[str] = []
    for group in grouped:
        row = ""
        for column, text in sorted(group):
            if column <= len(row):
                # The estimate put this word at or inside the end of the last
                # one. One space, never none: run together, "Off Peak" becomes
                # "OffPeak" and matches no rule, and "Baseline Allowance" stops
                # being recognised as the row to skip -- so its kWh quantity is
                # read as a few hundred dollars of charges.
                row += " " + text
            else:
                row = row.ljust(column) + text
        out.append(row.rstrip())
    return "\n".join(out)


def pages_via_ocr(path: str | Path) -> list[str]:
    """Recognise a statement's pages as layout-preserved text."""
    source = Path(path)
    if not available():
        raise StatementError(
            f"{source.name} carries no readable text and recognition tools are not "
            f"installed; `brew install tesseract poppler` provides both"
        )

    with tempfile.TemporaryDirectory(prefix="nem-ocr-") as scratch:
        images = _render(source, Path(scratch))
        if not images:
            raise StatementError(f"{source.name} produced no pages to recognise")
        return [_as_layout(_words(image)) for image in images]
