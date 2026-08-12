"""Effective-dated vendored data, for every dataset that has vintages.

Rates change on a date and stay changed until the next one, so pricing a moment
means finding the version in force *then* -- not the newest one on disk. Getting
that wrong is quiet: a January bill priced with March's rates looks entirely
plausible and is wrong by whatever moved in between, which for PG&E's public
purpose programs charge in early 2026 was over two cents a kilowatt-hour.

The rule is the same everywhere, so it lives here rather than being reimplemented
per dataset:

* a dataset is a directory of ``<effective>.toml`` files, one per vintage
* the version in force on a date is the latest one effective on or before it
* a date before the earliest vintage **raises** rather than borrowing it

That last point is the whole value of the mechanism. Silently reaching backwards
would price a period with rates that had not been adopted yet; raising says
exactly which vintage is missing, and the regenerators can then go and fetch it.

Datasets that carry their own vintage axis do not need this: export-rate matrices
are keyed by NBT vintage and year, the ACC Plus adder by interconnection year,
the NSC series by true-up month. Those already answer "which value applies" from
inside one file. This is for the ones whose whole contents are replaced when a
utility or a CCA reprices.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from importlib.resources import files
from typing import Any

from ..errors import DataError


@dataclass(frozen=True, slots=True)
class Version:
    """One effective-dated version of a dataset."""

    effective: date
    raw: dict[str, Any]

    @property
    def advice_letter(self) -> str:
        return str(self.raw.get("advice_letter", ""))

    @property
    def source_url(self) -> str:
        return str(self.raw.get("source_url", ""))


@lru_cache(maxsize=32)
def versions(relative: str) -> tuple[Version, ...]:
    """Every vendored vintage under ``relative``, oldest first.

    Accepts a directory of dated files, and also a bare ``<name>.toml`` so a
    dataset that has only ever had one vintage does not need a directory. The
    single-file form still has to declare its own ``effective`` date, because a
    version with no date cannot be placed in time and would have to be assumed
    current -- which is the assumption this module exists to remove.
    """
    resource = files("nem_rates.data")
    for part in relative.split("/"):
        resource = resource / part

    entries = []
    if resource.is_dir():
        entries = [e for e in resource.iterdir() if e.name.endswith(".toml")]
    elif resource.is_file():
        entries = [resource]

    found: list[Version] = []
    for entry in entries:
        raw = tomllib.loads(entry.read_text(encoding="utf-8"))
        stamp = raw.get("effective")
        if stamp is None:
            raise DataError(
                f"{relative}/{entry.name} declares no 'effective' date, so it cannot "
                f"be placed in time; every vendored version must say when it took force"
            )
        found.append(Version(date.fromisoformat(str(stamp)), raw))
    if not found:
        raise DataError(f"no vendored data at {relative}")
    return tuple(sorted(found, key=lambda v: v.effective))


def load(relative: str, on: date, *, label: str | None = None) -> Version:
    """The version in force on ``on``.

    Raises when ``on`` predates every vintage, naming the earliest and how many
    exist, since the fix is to vendor the missing one rather than to widen a
    lookup.
    """
    available = versions(relative)
    applicable = [v for v in available if v.effective <= on]
    if not applicable:
        raise DataError(
            f"{label or relative}: no version effective on or before {on}; "
            f"earliest vendored is {available[0].effective} "
            f"({len(available)} vintage{'s' if len(available) != 1 else ''} on disk). "
            f"Vendor the vintage covering {on} to price it."
        )
    return applicable[-1]


def coverage(relative: str) -> tuple[date, date | None]:
    """``(earliest effective, latest effective)`` for a dataset."""
    available = versions(relative)
    return available[0].effective, available[-1].effective
