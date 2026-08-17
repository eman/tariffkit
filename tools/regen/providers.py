"""Who publishes what, and where to get it.

PG&E is not the only utility and MCE is not the only CCA, so every
publisher-specific fact lives here rather than being spelled into an extractor.
Adding a second utility or a second CCA is an entry in one of these tables plus
whatever its documents need; it is not a change to the parsing code, which works
off the shapes in :mod:`tools.regen.sheets`.

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

from .sheets import ExtractionError

#: Where a utility files a rate change. An advice letter carries the sheets it
#: revises, so it is how a superseded vintage is recovered: the tariff book only
#: ever serves what is current.
ADVICE_LETTER_URL = "https://www.pge.com/tariffs/assets/pdf/adviceletter/ELEC_{number}.pdf"

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
    identifier: str
    short_name: str
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
    #: Template for one of this utility's advice-letter filings, if it publishes
    #: them at a predictable address. ``{number}`` is e.g. "7797-E".
    advice_letter_url: str = ""
    #: Slug -> how the sheet header spells the schedule, where that differs from
    #: the tariff name. PG&E bills "EV2-A" but heads its sheets "EV2".
    sheet_aliases: dict[str, str] = field(default_factory=dict)
    #: Structure stated in each schedule's Special Conditions but absent from
    #: its rate tables. This bootstraps a newly generated schedule; later
    #: vintages carry the verified structure forward.
    schedule_seasons: dict[str, dict[str, str]] = field(default_factory=dict)
    schedule_periods: dict[str, dict[str, object]] = field(default_factory=dict)
    baseline_schedules: frozenset[str] = frozenset()
    cca_drop_components: dict[str, tuple[str, ...]] = field(default_factory=dict)
    medical_exempt_components: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def sheet_name(self, slug: str) -> str:
        """How this schedule identifies itself in a sheet header."""
        return self.sheet_aliases.get(slug, self.schedule_names[slug])


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


@dataclass(frozen=True, slots=True)
class Program:
    """A residential overlay published as its own tariff schedule."""

    key: str
    utility: str
    data_slug: str
    source: Source


PACIFIC_GAS_AND_ELECTRIC = Utility(
    key="pge",
    identifier="pacific_gas_and_electric",
    short_name="PG&E",
    name="Pacific Gas and Electric Company",
    schedules={
        "e1": Source("https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-1.pdf"),
        "eelec": Source("https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-ELEC.pdf"),
        "etouc": Source(
            "https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-TOU-C.pdf"
        ),
        "etoud": Source(
            "https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-TOU-D.pdf"
        ),
        "ev2a": Source(
            "https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_EV2%20(Sch).pdf"
        ),
    },
    schedule_names={
        "e1": "E-1",
        "eelec": "E-ELEC",
        "etouc": "E-TOU-C",
        "etoud": "E-TOU-D",
        "ev2a": "EV2-A",
    },
    export_adder=Source("https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_NBT.pdf"),
    export_rates=Source(
        "https://www.pge.com/assets/pge/docs/vanities/PGE-Solar-Billing-Plan-Export-Rates.zip"
    ),
    franchise_fees=Source(
        "https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-FFS.pdf"
    ),
    nsc_rates=Source("https://www.pge.com/assets/pge/docs/clean-energy/solar/AB920-RateTable.pdf"),
    advice_letter_url=ADVICE_LETTER_URL,
    sheet_aliases={"ev2a": "EV2"},
    schedule_seasons={
        "e1": {"summer_start": "06-01", "summer_end": "09-30"},
        "etoud": {"summer_start": "06-01", "summer_end": "09-30"},
    },
    schedule_periods={
        "e1": {"peak": []},
        "etoud": {"peak": [[17, 20]], "peak_weekdays_only": True},
    },
    baseline_schedules=frozenset({"e1"}),
    cca_drop_components={
        "e1": ("generation", "bundled_pcia"),
        "etoud": ("generation", "bundled_pcia"),
    },
    medical_exempt_components={
        "e1": ("wildfire_fund_charge",),
        "etouc": ("wildfire_fund_charge",),
    },
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

D_CARE = Program(
    key="dcare",
    utility="pacific_gas_and_electric",
    data_slug="pge",
    source=Source("https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_D-CARE.pdf"),
)
D_MEDICAL = Program(
    key="dmedical",
    utility="pacific_gas_and_electric",
    data_slug="pge",
    source=Source("https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_D-MEDICAL.pdf"),
)
MEDICAL_BASELINE = Program(
    key="medicalbaseline",
    utility="pacific_gas_and_electric",
    data_slug="pge",
    source=Source("https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_RULES_19.pdf"),
)
E_RSMART = Program(
    key="ersmart",
    utility="pacific_gas_and_electric",
    data_slug="pge",
    source=Source("https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-RSMART.pdf"),
)


@dataclass(frozen=True, slots=True)
class Tax:
    """A statutory per-kWh surcharge, published by a tax authority.

    Neither a utility nor a CCA: it is imposed on energy consumed regardless of
    who supplies it, and is published as a numbered notice rather than a tariff
    sheet. Registered here so it is regenerated and watched like everything else
    rather than being the one number nobody looks at.
    """

    key: str
    name: str
    jurisdiction: str
    #: Where a numbered notice lives. ``{notice}`` is e.g. "l1020".
    notice_url: str
    #: Every notice known to state this rate, oldest first. CDTFA issues one
    #: only when the rate changes, so this is the list of vintages that exist --
    #: adding a year is a registry edit, the same shape as adding a schedule.
    notices: tuple[str, ...] = ()

    @property
    def latest_notice(self) -> str:
        if not self.notices:
            # Reached by adding a surcharge to the registry and not the notice
            # that publishes its rate. `regen tax` falls back to this when no
            # --notice is passed, so the bare IndexError would surface far from
            # the omission that caused it.
            raise ExtractionError(
                f"{self.key} lists no notices, so there is nothing to regenerate from; "
                f"add the notice number to its 'notices' in regen/providers.py, "
                f"or pass --notice to name one directly"
            )
        return self.notices[-1]

    def url_for(self, notice: str) -> str:
        """Where a notice lives. CDTFA prints "L-1020" but files it as l1020."""
        return self.notice_url.format(notice=notice.lower().replace("-", ""))


CA_ENERGY_RESOURCES = Tax(
    key="ca_energy_resources",
    name="California Energy Resources (Electrical Energy) Surcharge",
    jurisdiction="CA",
    notice_url="https://cdtfa.ca.gov/formspubs/{notice}.pdf",
    notices=("L-971", "L-1020"),
)

UTILITIES: dict[str, Utility] = {PACIFIC_GAS_AND_ELECTRIC.key: PACIFIC_GAS_AND_ELECTRIC}
TAXES: dict[str, Tax] = {CA_ENERGY_RESOURCES.key: CA_ENERGY_RESOURCES}
CCAS: dict[str, Cca] = {MCE.key: MCE}
PROGRAMS: dict[str, Program] = {
    program.key: program for program in (D_CARE, D_MEDICAL, MEDICAL_BASELINE, E_RSMART)
}


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
