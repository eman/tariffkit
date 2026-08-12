"""Regenerating the vendored rate data from what publishers publish.

Every price this library returns comes from data under ``nem_rates/data``, and
there is no runtime network access, so keeping prices correct is entirely a
matter of keeping that data current. This package is how.

It ships *inside* the library rather than sitting in a ``tools/`` directory
because the data does: a released wheel that carries rates a user cannot refresh
is only useful until the next advice letter. ``nem-rates regen`` is the entry
point.

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

Publishers are declared in :mod:`nem_rates.regen.providers`; adding a utility or
a CCA is an entry there rather than a change to any parser.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import accplus, cca, tariff
from .emit import Result
from .fetch import fetch
from .providers import CCAS, UTILITIES, Cca, Source, Utility
from .sheets import ExtractionError

#: Where downloaded documents are kept between runs.
DEFAULT_CACHE = Path.home() / ".cache" / "nem-rates" / "regen"

#: Datasets :func:`run` can build. Each maps to an entry in ``JOB_BUILDERS``.
DATASETS = ("tariff", "accplus", "cca")

#: The export-rate matrices are regenerated too, but from a 843 MB archive of
#: CSVs rather than a published PDF, so they have their own entry point --
#: ``python -m nem_rates.regen.export``. Naming it here alongside the rest would
#: let ``run("export")`` look supported when nothing can build it.
STANDALONE_DATASETS = ("export",)


@dataclass(frozen=True, slots=True)
class Job:
    """One document to regenerate one dataset from."""

    label: str
    source: Source
    run: Callable[[Path, bool], Result]


def _tariff_runner(util: Utility, slug: str) -> Callable[[Path, bool], Result]:
    def run_one(pdf: Path, check: bool) -> Result:
        return tariff.regenerate(util, slug, pdf, check=check)

    return run_one


def _accplus_runner(util: Utility) -> Callable[[Path, bool], Result]:
    def run_one(pdf: Path, check: bool) -> Result:
        return accplus.regenerate(util, pdf, check=check)

    return run_one


def _cca_runner(provider: Cca) -> Callable[[Path, bool], Result]:
    def run_one(pdf: Path, check: bool) -> Result:
        return cca.regenerate(provider, pdf, check=check)

    return run_one


def _tariff_jobs(provider: str | None) -> Iterator[Job]:
    for key, util in sorted(UTILITIES.items()):
        if provider and provider not in (key, *util.schedules):
            continue
        for slug, source in sorted(util.schedules.items()):
            if provider in util.schedules and provider != slug:
                continue
            yield Job(
                f"{key}/{slug}",
                source,
                _tariff_runner(util, slug),
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


def _cca_jobs(provider: str | None) -> Iterator[Job]:
    for key, provider_def in sorted(CCAS.items()):
        if provider and provider != key:
            continue
        yield Job(
            key,
            provider_def.rate_card,
            _cca_runner(provider_def),
        )


JOB_BUILDERS: dict[str, Callable[[str | None], Iterator[Job]]] = {
    "tariff": _tariff_jobs,
    "accplus": _accplus_jobs,
    "cca": _cca_jobs,
}


def jobs(dataset: str, provider: str | None = None) -> list[Job]:
    """Every regeneration job for ``dataset``, optionally narrowed to a provider."""
    if dataset not in JOB_BUILDERS:
        known = ", ".join(DATASETS)
        if dataset in STANDALONE_DATASETS:
            raise ExtractionError(
                f"{dataset!r} is regenerated by `python -m nem_rates.regen.{dataset}`, "
                f"not through run(); it reads an archive rather than a published PDF"
            )
        raise ExtractionError(f"unknown dataset {dataset!r}; known: {known}")
    found = list(JOB_BUILDERS[dataset](provider))
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
) -> list[Result]:
    """Regenerate ``dataset``, returning one result per document."""
    selected = jobs(dataset, provider)
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
