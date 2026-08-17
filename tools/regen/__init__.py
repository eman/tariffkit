"""Regenerating the vendored rate data from what publishers publish.

Every price this library returns comes from data under ``tariffkit/data``, and
there is no runtime network access, so keeping prices correct is entirely a
matter of keeping that data current. This package is how.

It remains in the repository's ``tools/`` tree and is deliberately excluded
from the runtime distribution. Maintainers run ``python -m tools.regen`` to
refresh the vendored data before publishing a release.

Four datasets, and what proves each one right:

============ ================================ =====================================
dataset      reads                            checked by
============ ================================ =====================================
tariff       a utility's retail tariff sheets components sum to published totals,
                                              then the library prices the result
export       the hourly export-rate archive   every cell of a lossless collapse
accplus      the export tariff's adder table  the library reads back every adder
cca          a CCA's generation rate card     both seasons, matching period sets
============ ================================ =====================================

The check matters more than the extraction. A generator writes key names and the
library reads them back with a second, independent set of literals -- two
encodings of one schema that can drift apart silently. So nothing is written
unless the rendered file survives being read by the code that will consume it.

Publishers are declared in :mod:`tools.regen.providers`; adding a utility or
a CCA is an entry there rather than a change to any parser.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import accplus, cca, filings, nsc, program, tariff, tax
from .emit import DEFAULT_CACHE, Result
from .fetch import fetch
from .providers import CCAS, PROGRAMS, TAXES, UTILITIES, Cca, Program, Source, Tax, Utility
from .sheets import ExtractionError

#: Where downloaded documents are kept between runs.

#: Datasets :func:`run` can build. Each maps to an entry in ``JOB_BUILDERS``.
DATASETS = ("tariff", "program", "accplus", "nsc", "cca", "tax")

#: Where to start looking for the filing that set a vintage. Covers the recent
#: past; older dates widen the search rather than failing.
DEFAULT_SCAN = (7500, 7900)
#: How much further back each widening reaches.
SCAN_STEP = 200
#: A bound on widening, so a date the utility never filed for stops rather than
#: walking to advice letter one.
MAX_SCAN_WIDENINGS = 4

#: The export-rate matrices are regenerated too, but from a 843 MB archive of
#: CSVs rather than a published PDF, so they have their own entry point --
#: ``python -m tools.regen.export``. Naming it here alongside the rest would
#: let ``run("export")`` look supported when nothing can build it.
STANDALONE_DATASETS = ("export",)


@dataclass(frozen=True, slots=True)
class Job:
    """One document to regenerate one dataset from."""

    label: str
    source: Source
    run: Callable[[Path, bool], Result]


def _tariff_runner(
    util: Utility, slug: str, cache: Path, refresh: bool
) -> Callable[[Path, bool], Result]:
    def run_one(pdf: Path, check: bool) -> Result:
        # The schedule sheet arrives already fetched; E-FFS is fetched inside,
        # so the run's cache and --refresh policy has to travel with it or that
        # second document quietly ignores both.
        return tariff.regenerate(util, slug, pdf, check=check, cache=cache, refresh=refresh)

    return run_one


def _accplus_runner(util: Utility) -> Callable[[Path, bool], Result]:
    def run_one(pdf: Path, check: bool) -> Result:
        return accplus.regenerate(util, pdf, check=check)

    return run_one


def _nsc_runner(util: Utility) -> Callable[[Path, bool], Result]:
    def run_one(pdf: Path, check: bool) -> Result:
        return nsc.regenerate(util, pdf, check=check)

    return run_one


def _cca_runner(provider: Cca) -> Callable[[Path, bool], Result]:
    def run_one(pdf: Path, check: bool) -> Result:
        return cca.regenerate(provider, pdf, check=check)

    return run_one


def _program_runner(provider: Program) -> Callable[[Path, bool], Result]:
    def run_one(pdf: Path, check: bool) -> Result:
        return program.regenerate(provider, pdf, check=check)

    return run_one


def _tariff_jobs(
    provider: str | None, cache: Path = DEFAULT_CACHE, refresh: bool = False
) -> Iterator[Job]:
    for key, util in sorted(UTILITIES.items()):
        if provider and provider not in (key, *util.schedules):
            continue
        for slug, source in sorted(util.schedules.items()):
            if provider in util.schedules and provider != slug:
                continue
            yield Job(
                f"{key}/{slug}",
                source,
                _tariff_runner(util, slug, cache, refresh),
            )


def _accplus_jobs(provider: str | None) -> Iterator[Job]:
    for key, util in sorted(UTILITIES.items()):
        if (provider and provider != key) or util.export_adder is None:
            continue
        yield Job(
            f"{key}/accplus",
            util.export_adder,
            _accplus_runner(util),
        )


def _nsc_jobs(provider: str | None) -> Iterator[Job]:
    for key, util in sorted(UTILITIES.items()):
        if (provider and provider != key) or util.nsc_rates is None:
            continue
        yield Job(f"{key}/nsc", util.nsc_rates, _nsc_runner(util))


def _tax_runner(provider: Tax, notice: str) -> Callable[[Path, bool], Result]:
    def run_one(pdf: Path, check: bool) -> Result:
        return tax.regenerate(provider, pdf, check=check, notice=notice)

    return run_one


def _tax_jobs(provider: str | None) -> Iterator[Job]:
    for key, entry in sorted(TAXES.items()):
        if provider and provider not in (key, entry.jurisdiction.lower()):
            continue
        # One job per notice: each states one calendar year, and a rate that
        # carried forward has no notice of its own, so the notices are exactly
        # the vintages that exist.
        for notice in entry.notices:
            yield Job(
                f"{key}/{notice}",
                Source(entry.url_for(notice)),
                _tax_runner(entry, notice),
            )


def _cca_jobs(provider: str | None) -> Iterator[Job]:
    for key, provider_def in sorted(CCAS.items()):
        if provider and provider != key:
            continue
        yield Job(
            key,
            provider_def.rate_card,
            _cca_runner(provider_def),
        )


def _program_jobs(provider: str | None) -> Iterator[Job]:
    for key, entry in sorted(PROGRAMS.items()):
        if provider and provider != key:
            continue
        yield Job(
            f"{entry.data_slug}/{key}",
            entry.source,
            _program_runner(entry),
        )


#: Only the tariff builder needs the run's cache policy, because only it fetches
#: a second document of its own.
JOB_BUILDERS: dict[str, Callable[..., Iterator[Job]]] = {
    "tariff": _tariff_jobs,
    "program": _program_jobs,
    "accplus": _accplus_jobs,
    "nsc": _nsc_jobs,
    "cca": _cca_jobs,
    "tax": _tax_jobs,
}


def jobs(
    dataset: str,
    provider: str | None = None,
    cache: Path = DEFAULT_CACHE,
    refresh: bool = False,
) -> list[Job]:
    """Every regeneration job for ``dataset``, optionally narrowed to a provider."""
    if dataset not in JOB_BUILDERS:
        known = ", ".join(DATASETS)
        if dataset in STANDALONE_DATASETS:
            raise ExtractionError(
                f"{dataset!r} is regenerated by `python -m tools.regen.{dataset}`, "
                f"not through run(); it reads an archive rather than a published PDF"
            )
        raise ExtractionError(f"unknown dataset {dataset!r}; known: {known}")
    builder = JOB_BUILDERS[dataset]
    found = list(builder(provider, cache, refresh) if dataset == "tariff" else builder(provider))
    if not found:
        raise ExtractionError(
            f"no {dataset} sources for provider {provider!r}; "
            f"registered utilities: {', '.join(sorted(UTILITIES))}; "
            f"CCAs: {', '.join(sorted(CCAS))}"
        )
    return found


def run(
    dataset: str,
    *,
    provider: str | None = None,
    pdf: Path | None = None,
    check: bool = False,
    cache: Path = DEFAULT_CACHE,
    refresh: bool = False,
    advice_letter: str | None = None,
    for_date: date | None = None,
    scan: tuple[int, int] | None = None,
) -> list[Result]:
    """Regenerate ``dataset``, returning one result per document.

    ``advice_letter`` rebuilds a *superseded* vintage instead of the current one.
    The tariff book only ever serves what is in force now, so a historical
    snapshot comes from the filing that adopted it -- one filing carries every
    schedule the utility revised that day, which is why this pairs naturally with
    regenerating all of them at once.
    """
    if for_date is not None:
        advice_letter = _filing_for_date(provider, for_date, cache, scan, refresh)
    if advice_letter is not None:
        return _run_advice_letter(dataset, advice_letter, provider, check, cache, refresh)
    selected = jobs(dataset, provider, cache, refresh)
    if pdf is not None and len(selected) > 1:
        raise ExtractionError(
            f"--pdf takes one document, but {dataset} with this provider has "
            f"{len(selected)}: {', '.join(j.label for j in selected)}"
        )

    results: list[Result] = []
    for job in selected:
        if pdf is None and not job.source.fetchable:
            # Not stale, just unknowable from here. Failing would make a
            # scheduled check permanently red and train everyone to ignore it.
            results.append(
                Result(
                    job.label,
                    changed=False,
                    failed=False,
                    messages=(f"SKIPPED, needs --pdf: {job.source.blocked_note}",),
                )
            )
            continue
        try:
            path = pdf or fetch(
                job.source, cache / f"{job.label.replace('/', '-')}.pdf", refresh=refresh
            )
            results.append(job.run(path, check))
        except ExtractionError as exc:
            results.append(Result(job.label, changed=False, failed=True, messages=(str(exc),)))
    return results


def _filing_for_date(
    provider: str | None, on: date, cache: Path, scan: tuple[int, int] | None, refresh: bool
) -> str:
    """Which filing set the rates in force on ``on``.

    Indexes the utility's advice letters if it has not already; the index is
    cached, so this is slow once and instant afterwards.

    A date older than :data:`DEFAULT_SCAN` reaches back on its own, a
    :data:`SCAN_STEP` block at a time up to :data:`MAX_SCAN_WIDENINGS`, because
    the caller knows the date it wants priced and has no way to know which
    advice letter numbers that lands on. Widening is skipped when ``scan`` was
    given explicitly: that is the caller pinning a range, and quietly searching
    outside it would defeat the point of passing it.
    """
    for key, util in sorted(UTILITIES.items()):
        if not util.advice_letter_url or (provider and provider not in (key, *util.schedules)):
            continue
        root = cache / "al"
        indexed = filings.load_index(root, key)
        if scan or not indexed:
            lo, hi = scan or DEFAULT_SCAN
            print(f"  indexing {key} filings {lo}-{hi} (cached after the first run)")
            indexed = filings.build_index(util, lo, hi, root, refresh=refresh)
        sheet = (
            util.sheet_name(provider)
            if provider in util.schedules
            else next(iter(util.schedule_names.values()))
        )
        found = filings.filing_for(util, sheet, on, indexed)

        widenings = 0
        reached = DEFAULT_SCAN[0]
        while found is None and scan is None and widenings < MAX_SCAN_WIDENINGS:
            widenings += 1
            lo = DEFAULT_SCAN[0] - widenings * SCAN_STEP
            hi = DEFAULT_SCAN[0] - (widenings - 1) * SCAN_STEP - 1
            print(f"  nothing in force on {on} yet; reaching back to {lo}-{hi}")
            # refresh only ever applies to the first pass: a widening probes
            # numbers the index has never held, and re-fetching with refresh set
            # would discard the block just indexed instead of adding to it.
            indexed = filings.build_index(util, lo, hi, root, refresh=False)
            found = filings.filing_for(util, sheet, on, indexed)
            reached = lo

        if found is None:
            span = sorted({v for f in indexed.values() for v in f.schedules.values()})
            how_far = (
                f"pinned to {scan[0]}-{scan[1]}"
                if scan
                else f"reached back to {reached} over {widenings} widening(s)"
            )
            raise ExtractionError(
                f"no indexed {key} filing was in force for {sheet} on {on}; "
                f"indexed vintages run {span[0] if span else 'none'} to "
                f"{span[-1] if span else 'none'}, having {how_far}. "
                f"Pass --scan LO-HI to search a range directly."
            )
        print(f"  {on} -> filing {found.number} ({found.schedules.get(sheet)})")
        return found.number
    raise ExtractionError(f"no utility publishes advice letters for provider {provider!r}")


def _run_advice_letter(
    dataset: str, number: str, provider: str | None, check: bool, cache: Path, refresh: bool
) -> list[Result]:
    if dataset != "tariff":
        raise ExtractionError(
            f"--advice-letter rebuilds retail tariff vintages; {dataset!r} is not "
            f"published that way"
        )
    number = number.upper() if number.upper().endswith("-E") else f"{number}-E"
    results: list[Result] = []
    for key, util in sorted(UTILITIES.items()):
        if not util.advice_letter_url or (provider and provider not in (key, *util.schedules)):
            continue
        source = Source(util.advice_letter_url.format(number=number))
        path = fetch(source, cache / "al" / f"{number}.pdf", refresh=refresh)
        for slug in sorted(util.schedules):
            if provider in util.schedules and provider != slug:
                continue
            try:
                results.append(
                    tariff.regenerate(util, slug, path, check=check, cache=cache, refresh=refresh)
                )
            except ExtractionError as exc:
                # A filing revises only some schedules, so "not in here" is a
                # normal answer rather than a failure.
                results.append(
                    Result(f"{key}/{slug}", changed=False, failed=False, messages=(str(exc),))
                )
    if not results:
        raise ExtractionError(f"no utility publishes advice letters for provider {provider!r}")
    return results


__all__ = [
    "DATASETS",
    "DEFAULT_CACHE",
    "STANDALONE_DATASETS",
    "Cca",
    "ExtractionError",
    "Job",
    "Result",
    "Utility",
    "jobs",
    "run",
]
