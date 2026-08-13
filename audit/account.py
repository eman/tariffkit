"""Which configuration priced which cycle.

Rate *data* is already effective-dated: ``versioned.load`` picks the snapshot in
force on a date and raises rather than reaching backwards. What is not dated is
the *account* -- the schedule it is on, whether a community choice aggregator
supplies its generation, its baseline territory, its PCIA vintage. Those change
over an account's life too, and a :class:`~nem_rates.config.Config` describes only
one moment of it. Pricing a 2026 January statement with a configuration written
for April prices E-TOU-C usage on EV2-A, which is off by tens of dollars and
looks entirely plausible.

So the account gets its own history: a base configuration and a list of epochs,
each starting on a date and overriding part of it.

Two decisions here are load-bearing, and both are refusals:

* A cycle that **straddles** an epoch boundary raises rather than picking a side.
  No single configuration priced it, and choosing one silently produces a
  believable delta that gets filed as a rounding mystery -- the same reasoning
  that makes ``versioned.load`` raise instead of borrowing a vintage.
* The statement is asked to **confirm** the epoch before anything is compared.
  A statement prints its own rate schedule, baseline territory and PCIA vintage,
  so a stale ``account.toml`` is detectable. Without that check a
  misconfiguration yields a wrong-but-coherent bill and the harness reports a
  fabricated defect with total confidence, which is worse than reporting nothing.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from nem_rates.billing import BillingPeriod
from nem_rates.config import CcaConfig, Config
from nem_rates.errors import ConfigError
from nem_rates.models import Supplier

from .errors import AccountError
from .statements import Statement

#: How a schedule is printed on a statement, mapped to what this library calls
#: it. PG&E prints marketing names -- "Time-of-Use (Peak Pricing 4 - 9 p.m.
#: Every Day)" is Schedule E-TOU-C -- and nothing on the bill states the tariff
#: code, so the correspondence has to be written down somewhere.
#:
#: Ordered, because the names overlap: E-ELEC is also a 4-9 p.m. time-of-use
#: plan, so its own name has to be recognised before the generic one.
PRINTED_SCHEDULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"EV\s*2-?A", re.I), "EV2-A"),
    (re.compile(r"E-?ELEC|Electric\s+Home", re.I), "E-ELEC"),
    (re.compile(r"Time-of-Use.*4\s*-\s*9", re.I), "E-TOU-C"),
)


def schedule_from_printed(printed: str) -> str | None:
    """The tariff a printed rate-schedule name refers to, if it is recognised."""
    for pattern, tariff in PRINTED_SCHEDULES:
        if pattern.search(printed):
            return tariff
    return None


@dataclass(frozen=True, slots=True)
class AccountEpoch:
    """A span of the account's life, from ``start`` until the next one begins."""

    start: date
    overrides: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""

    def apply(self, base: Config) -> Config:
        changes = dict(self.overrides)
        cca = changes.pop("cca", None)
        if cca is not None:
            try:
                changes["cca"] = CcaConfig(**dict(cca))
            except TypeError as exc:
                raise AccountError(
                    f"epoch starting {self.start}: bad [epoch.cca] table: {exc}"
                ) from exc
        try:
            return base.with_(**changes) if changes else base
        except (ConfigError, TypeError) as exc:
            raise AccountError(f"epoch starting {self.start}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class AccountHistory:
    base: Config
    epochs: tuple[AccountEpoch, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AccountHistory:
        base = Config.from_dict(dict(raw.get("base", {})))
        epochs = []
        for entry in raw.get("epoch", []):
            overrides = dict(entry)
            start = overrides.pop("from", None)
            note = str(overrides.pop("note", ""))
            if start is None:
                raise AccountError("every [[epoch]] needs a 'from' date")
            epochs.append(
                AccountEpoch(
                    start=start if isinstance(start, date) else date.fromisoformat(str(start)),
                    overrides=overrides,
                    note=note,
                )
            )
        return cls(base=base, epochs=tuple(sorted(epochs, key=lambda e: e.start)))

    @classmethod
    def from_toml(cls, path: str | Path) -> AccountHistory:
        source = Path(path)
        if not source.is_file():
            raise AccountError(
                f"no account history at {source}; copy audit/account.example.toml to "
                f"{source} and describe the account's schedule and supplier over time"
            )
        return cls.from_dict(tomllib.loads(source.read_text(encoding="utf-8")))

    def epochs_in(self, period: BillingPeriod) -> tuple[AccountEpoch, ...]:
        """Every epoch that was in force at any point during ``period``."""
        started = [e for e in self.epochs if e.start <= period.end]
        if not started:
            return ()
        in_force = [e for e in started if e.start <= period.start]
        during = [e for e in started if period.start < e.start <= period.end]
        return tuple((in_force[-1:] if in_force else []) + during)

    def config_for(self, period: BillingPeriod) -> Config:
        """The configuration that priced this whole cycle.

        Raises when the cycle spans a change, because no single configuration
        did, and guessing produces a plausible number rather than an obvious
        failure.
        """
        applicable = self.epochs_in(period)
        if not applicable:
            earliest = self.epochs[0].start if self.epochs else None
            raise AccountError(
                f"no account epoch covers {period.start}..{period.end}"
                + (f"; the earliest begins {earliest}" if earliest else "; none are configured")
            )
        if len(applicable) > 1:
            changes = ", ".join(
                f"{e.start}" + (f" ({e.note})" if e.note else "") for e in applicable[1:]
            )
            raise AccountError(
                f"the cycle {period.start}..{period.end} spans an account change ({changes}), "
                f"so no single configuration priced it; this statement has to be checked by hand"
            )
        return applicable[0].apply(self.base)


def check_against_statement(config: Config, statement: Statement) -> list[str]:
    """Disagreements between the configured epoch and what the bill says it was.

    Returned rather than raised so a caller can report all of them at once. Every
    one means the harness was about to price the cycle as something it was not.
    """
    problems: list[str] = []

    if statement.rate_schedule:
        printed = schedule_from_printed(statement.rate_schedule)
        if printed is None:
            problems.append(
                f"the statement's rate schedule {statement.rate_schedule!r} matches no known "
                f"tariff, so the configured {config.tariff!r} cannot be confirmed; add it to "
                f"PRINTED_SCHEDULES"
            )
        elif printed != config.tariff:
            problems.append(
                f"configured for {config.tariff} but the statement was billed on {printed} "
                f"({statement.rate_schedule!r})"
            )

    supplied_by_cca = bool(statement.cca_name)
    if supplied_by_cca and config.supplier is not Supplier.CCA:
        problems.append(
            f"configured as {config.supplier.value} but {statement.cca_name} supplied generation"
        )
    elif not supplied_by_cca and config.supplier is Supplier.CCA:
        problems.append("configured for CCA generation but the statement has no generation page")

    if config.cca is not None and statement.cca_name:
        card = (config.cca.rate_card or config.cca.name or "").lower()
        if card and card != statement.cca_name.lower():
            problems.append(
                f"configured for CCA {card!r} but the statement is from {statement.cca_name!r}"
            )
        if (
            statement.pcia_vintage is not None
            and config.cca.pcia_vintage is not None
            and statement.pcia_vintage != config.cca.pcia_vintage
        ):
            problems.append(
                f"configured PCIA vintage {config.cca.pcia_vintage} but the statement is billed "
                f"the {statement.pcia_vintage} vintage"
            )

    if (
        statement.baseline_territory
        and config.baseline_territory
        and statement.baseline_territory != config.baseline_territory
    ):
        problems.append(
            f"configured baseline territory {config.baseline_territory!r} but the statement "
            f"says {statement.baseline_territory!r}"
        )

    return problems


def describe(history: AccountHistory, periods: Sequence[BillingPeriod]) -> list[str]:
    """One line per cycle naming the configuration it resolves to."""
    lines = []
    for period in periods:
        try:
            config = history.config_for(period)
        except AccountError as exc:
            lines.append(f"{period.start}..{period.end}  UNPRICEABLE: {exc}")
            continue
        supplier = config.cca.name or config.cca.rate_card if config.cca else "bundled"
        lines.append(f"{period.start}..{period.end}  {config.tariff} / {supplier}")
    return lines
