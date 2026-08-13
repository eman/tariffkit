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

#: Tesseract's page segmentation mode. 6 = "assume a single uniform block of
#: text", which keeps rows intact. The default tries to detect columns and
#: reorders them, which destroys the left-to-right run of a charge row.
PSM = "6"

#: Words closer together than this fraction of a character width are one field.
#: The parser splits fields on two or more spaces, so the grid has to preserve
#: the difference between a gap inside a label and a gap between columns.
MIN_GAP = 2


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


def _words(image: Path) -> list[tuple[int, int, int, str]]:
    """Recognised words as (line id, left, width, text), in reading order."""
    result = subprocess.run(
        ["tesseract", str(image), "stdout", "--psm", PSM, "tsv"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise StatementError(
            f"recognition failed on {image.name}: {result.stderr.decode('utf-8', 'replace')[:200]}"
        )

    rows: list[tuple[int, int, int, str]] = []
    reader = csv.DictReader(result.stdout.decode("utf-8", "replace").splitlines(), delimiter="\t")
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            # block/paragraph/line together identify one printed row; page_num
            # is constant here because each image is one page.
            line = (
                int(row["block_num"]) * 100_000 + int(row["par_num"]) * 1_000 + int(row["line_num"])
            )
            rows.append((line, int(row["left"]), int(row["width"]), text))
        except (KeyError, ValueError):
            continue
    return rows


def _as_layout(words: list[tuple[int, int, int, str]]) -> str:
    """Rebuild a character grid from word boxes.

    The parser reads column positions, not just words, so the horizontal
    geometry has to survive. Each word is placed at the column its pixel offset
    implies, using the median character width as the scale -- which is what makes
    "Distribution" and its right-aligned amount stay in different fields.
    """
    if not words:
        return ""

    widths = sorted(width / len(text) for _, _, width, text in words if text)
    char_width = widths[len(widths) // 2] or 1.0

    lines: dict[int, list[tuple[int, str]]] = {}
    for line, left, _, text in words:
        lines.setdefault(line, []).append((round(left / char_width), text))

    out: list[str] = []
    for line in sorted(lines):
        row = ""
        for column, text in sorted(lines[line]):
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
