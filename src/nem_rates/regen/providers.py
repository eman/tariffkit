"""Who publishes what, and where to get it.

PG&E is not the only utility and MCE is not the only CCA, so every
publisher-specific fact lives here rather than being spelled into an extractor.
Adding a second utility or a second CCA is an entry in one of these tables plus
whatever its documents need; it is not a change to the parsing code, which works
off the shapes in :mod:`nem_rates.regen.sheets`.

What a new entry has to supply:

* ``key`` -- the slug used on the command line and in the data directory.
* the URL of each document, and whether it can actually be fetched by a script.

That last one is not a detail. Publishers put their rate cards behind wildly
different infrastructure, and the differences are arbitrary: MCE's CDN answers
urllib and curl with 403 whatever headers they send, and answers httpx with 200
and the file. A source that genuinely cannot be fetched is still regenerable
from a file saved by hand, so :class:`Source` records the difference instead of
pretending every publisher behaves the same.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Sent when fetching. Some publishers reject the default urllib agent outright.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class Source:
    """One published document."""

    url: str
    #: False when the publisher blocks scripted fetches. The document is then
    #: supplied with --pdf, and --download says so rather than failing obscurely.
    fetchable: bool = True
    #: Why it is not fetchable, shown to the user when they ask to download it.
    blocked_note: str = ""


@dataclass(frozen=True, slots=True)
class Utility:
    """A distribution utility: retail tariffs, export rates, export adders."""

    key: str
    name: str
    #: Retail schedule slug -> its tariff sheet. Slugs match the data
    #: directories: the tariff name with punctuation stripped.
    schedules: dict[str, Source] = field(default_factory=dict)
    #: Schedule slug -> the tariff name as printed, e.g. "eelec" -> "E-ELEC".
    schedule_names: dict[str, str] = field(default_factory=dict)
    #: The tariff carrying the export adder table, if the utility publishes one.
    export_adder: Source | None = None
    #: The archive of hourly export-rate matrices, if published.
    export_rates: Source | None = None
    #: The franchise fee surcharge schedule. A separate document from the retail
    #: sheets, but its values live inside a tariff snapshot's [cca] table.
    franchise_fees: Source | None = None
    #: The Net Surplus Compensation series, published as a standing table.
    nsc_rates: Source | None = None


@dataclass(frozen=True, slots=True)
class Cca:
    """A Community Choice Aggregator, which supplies generation only."""

    key: str
    name: str
    #: The utility whose delivery service it pairs with, and whose schedule
    #: names its rate card uses.
    utility: str
    rate_card: Source
    #: How the rate card spells each schedule -> our slug. MCE writes "ELEC"
    #: for PG&E's E-ELEC and "EV2" for EV2-A, so this cannot be derived.
    schedule_aliases: dict[str, str] = field(default_factory=dict)
    #: Where the card states its own terms, recorded in the emitted comment.
    tariff_url: str = ""


PGE = Utility(
    key="pge",
    name="Pacific Gas and Electric",
    schedules={
        "eelec": Source("https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-ELEC.pdf"),
        "etouc": Source(
            "https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-TOU-C.pdf"
        ),
        "ev2a": Source(
            "https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_EV2%20(Sch).pdf"
        ),
    },
    schedule_names={"eelec": "E-ELEC", "etouc": "E-TOU-C", "ev2a": "EV2-A"},
    export_adder=Source("https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_NBT.pdf"),
    export_rates=Source(
        "https://www.pge.com/assets/pge/docs/vanities/PGE-Solar-Billing-Plan-Export-Rates.zip"
    ),
    franchise_fees=Source(
        "https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-FFS.pdf"
    ),
    nsc_rates=Source("https://www.pge.com/assets/pge/docs/clean-energy/solar/AB920-RateTable.pdf"),
)

MCE = Cca(
    key="mce",
    name="Marin Clean Energy",
    utility="pge",
    rate_card=Source(
        "https://mcecleanenergy.org/wp-content/uploads/2025/09/"
        "MCE-website-rate-table_RES_as-of-4.1.26.pdf",
        # Downloadable, but only by httpx: MCE's CDN answers urllib and curl
        # with 403 no matter what headers they send. See fetch._get_with_httpx.
        fetchable=True,
    ),
    schedule_aliases={"ELEC": "eelec", "ETOUC": "etouc", "EV2": "ev2a"},
    tariff_url=(
        "https://mcecleanenergy.org/wp-content/uploads/2024/12/MCE-Solar-Billing-Plan-Tariff_120424.pdf"
    ),
)

UTILITIES: dict[str, Utility] = {PGE.key: PGE}
CCAS: dict[str, Cca] = {MCE.key: MCE}


def utility(key: str) -> Utility:
    try:
        return UTILITIES[key]
    except KeyError:
        raise ExtractionKeyError("utility", key, sorted(UTILITIES)) from None


def cca(key: str) -> Cca:
    try:
        return CCAS[key]
    except KeyError:
        raise ExtractionKeyError("CCA", key, sorted(CCAS)) from None


class ExtractionKeyError(KeyError):
    """An unregistered provider, named alongside the ones that do exist."""

    def __init__(self, kind: str, key: str, known: list[str]) -> None:
        super().__init__(f"unknown {kind} {key!r}; registered: {', '.join(known)}")
