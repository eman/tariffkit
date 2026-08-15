"""Regenerating the franchise fee surcharge from Schedule E-FFS.

A CCA or Direct Access customer pays this and a bundled customer does not, so it
sits inside the tariff snapshot's ``[cca]`` table -- but it is published in a
*different* schedule, which is why it was carried forward from the previous
snapshot rather than regenerated. Carrying it forward meant PG&E could reissue
E-FFS and nothing would notice: the value is live rate data, read by
``RetailTariff`` on every CCA price.

The sheet prints vintages three to a block, with one row per customer class::

    Customer Class DA/CCA Franchise Fee Surcharge Rate per kWh
    Pre-2009 Vintage 2009 Vintage 2010 Vintage
    Residential $0.00086 (R) $0.00064 (R) $0.00061 (R)
    Small L&P   $0.00083 (R) $0.00062 (R) $0.00059 (R)
    ...
    2011 Vintage 2012 Vintage 2013 Vintage
    Residential $0.00060 (R) $0.00059 (R) $0.00059 (R)

so the vintages for a row come from the most recent header above it, and the
blocks repeat until the table runs out.

The Pre-2009 vintage is read but dropped: the PCIA table it is keyed alongside
has no pre-2009 entry, so such a customer cannot be priced anyway, and keeping
it would imply a completeness the pair does not have.
"""

from __future__ import annotations

import re

from .sheets import ExtractionError, Page, cells

#: The row this library needs. Every schedule it prices is residential.
DEFAULT_CUSTOMER_CLASS = "Residential"

#: The heading that identifies the table, used to find it rather than a page.
TABLE_HEADING = "Franchise Fee Surcharge Rate per kWh"

#: A header naming the vintages the next rows are priced for.
VINTAGE_HEADER = re.compile(r"(Pre-\d{4}|\d{4})\s+Vintage")

#: Surcharges have run from 0.00040 to 0.00093. Anything outside this came from
#: a neighbouring column or from prose.
PLAUSIBLE = (0.00001, 0.01)

#: Fewer than this and the table was truncated by a bad parse; the sheet has
#: carried eighteen vintages plus Pre-2009 for years.
MIN_VINTAGES = 10


def extract(pages: list[Page], customer_class: str = DEFAULT_CUSTOMER_CLASS) -> dict[int, float]:
    """``{vintage year: surcharge}`` for one customer class."""
    if not any(TABLE_HEADING in page.text for page in pages):
        raise ExtractionError(f"no page contains {TABLE_HEADING!r}")

    found: dict[int, float] = {}
    vintages: list[str] = []
    for page in pages:
        for raw in page.text.splitlines():
            line = raw.strip().rstrip("|").strip()
            if not line:
                continue
            headers = VINTAGE_HEADER.findall(line)
            if headers:
                vintages = headers
                continue
            if not line.lower().startswith(customer_class.lower()) or not vintages:
                continue
            values = cells(line)
            if len(values) != len(vintages):
                raise ExtractionError(
                    f"{customer_class} row has {len(values)} values for "
                    f"{len(vintages)} vintages {vintages}"
                )
            for vintage, value in zip(vintages, values, strict=True):
                if not PLAUSIBLE[0] <= value <= PLAUSIBLE[1]:
                    raise ExtractionError(
                        f"{customer_class} {vintage}: {value} is outside the plausible "
                        f"range {PLAUSIBLE}"
                    )
                if vintage.startswith("Pre-"):
                    # Read, then dropped: the PCIA table this is keyed alongside
                    # has no pre-2009 entry, so such a customer cannot be priced
                    # anyway and keeping it would imply a completeness the pair
                    # does not have.
                    continue
                found[int(vintage)] = value

    if len(found) < MIN_VINTAGES:
        raise ExtractionError(
            f"found only {len(found)} franchise fee vintages for {customer_class}; "
            f"expected at least {MIN_VINTAGES}"
        )
    return found
