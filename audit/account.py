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

A cycle that **straddles** an epoch boundary is split into segments and each is
priced under its own configuration, which is what the utility itself does: a
mid-cycle change prints as two blocks on one statement. This used to be refused
outright on the grounds that no single ``Config`` priced it -- true, but the
conclusion belonged in the engine, not in a refusal, and refusing skipped exactly
the cycles worth checking hardest.

One decision here is load-bearing, and it is a refusal:

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
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from nem_rates.billing import BillingPeriod
from nem_rates.billing.engine import Segment
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
    # The CCA prints the tariff code rather than the marketing name -- "ETOUC"
    # against PG&E's "Time-of-Use (Peak Pricing 4 - 9 p.m. Every Day)", and
    # "SBP EELEC" once on the Solar Billing Plan. Both name the same schedule,
    # and leaving the code unrecognised makes every CCA statement report that
    # its schedule could not be confirmed.
    (re.compile(r"E-?TOU-?C", re.I), "E-TOU-C"),
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

    def segments_for(self, period: BillingPeriod) -> list[Segment]:
        """The cycle broken into stretches, one per configuration in force.

        A cycle spanning an account change used to be refused here, on the
        grounds that no single ``Config`` priced it. That was true and the wrong
        conclusion: the utility prices each stretch separately and prints them
        as separate blocks, so the fix belonged in the engine rather than in a
        refusal. ``compute_segments`` does exactly that now.

        Refusing also skipped precisely the cycles worth checking hardest -- the
        ones where a schedule changed or solar was interconnected -- leaving a
        harness that verified only the quiet months.
        """
        applicable = self.epochs_in(period)
        if not applicable:
            earliest = self.epochs[0].start if self.epochs else None
            raise AccountError(
                f"no account epoch covers {period.start}..{period.end}"
                + (f"; the earliest begins {earliest}" if earliest else "; none are configured")
            )

        segments: list[Segment] = []
        for index, epoch in enumerate(applicable):
            start = max(epoch.start, period.start)
            if index + 1 < len(applicable):
                end = applicable[index + 1].start - timedelta(days=1)
            else:
                end = period.end
            segments.append(
                Segment(epoch.apply(self.base), BillingPeriod(start, min(end, period.end)))
            )
        return segments

    def config_for(self, period: BillingPeriod) -> Config:
        """The configuration in force at the end of the cycle.

        For everything that needs one description of the account rather than a
        priced bill: which tariff to print, what to check the statement's own
        wording against. Pricing goes through :meth:`segments_for`.
        """
        return self.segments_for(period)[-1].config


def check_against_statement(
    config: Config, statement: Statement, *, segments: Sequence[Segment] = ()
) -> list[str]:
    """Disagreements between the configured epoch and what the bill says it was.

    Returned rather than raised so a caller can report all of them at once. Every
    one means the harness was about to price the cycle as something it was not.

    ``segments`` matters on a statement covering a mid-cycle change: it prints
    one schedule per agreement, so comparing a single configured tariff against
    the first one printed reports a disagreement on a correctly configured
    account. Compared as sets, because the statement's ordering of its own
    blocks is not something to depend on.
    """
    problems: list[str] = []

    configured = {s.config.tariff for s in segments} or {config.tariff}
    printed_names = statement.printed_schedules or (
        (statement.rate_schedule,) if statement.rate_schedule else ()
    )
    # Not every "Rate Schedule:" on the page is an electric tariff. A combined
    # statement prints the gas schedule ("G1 XB Residential Service") and the
    # Solar Billing Plan pages carry a prose description of the rate. Requiring
    # every name to resolve reports those as unknown tariffs; requiring at least
    # one keeps the check that matters, which is that what was recognised is
    # what was configured.
    recognised = {
        tariff for tariff in (schedule_from_printed(name) for name in printed_names) if tariff
    }
    if printed_names and not recognised:
        problems.append(
            f"none of the statement's rate schedules {list(printed_names)} matches a known "
            f"tariff, so the configured {sorted(configured)} cannot be confirmed; add it to "
            f"PRINTED_SCHEDULES"
        )
    elif recognised and recognised != configured:
        problems.append(
            f"configured for {sorted(configured)} but the statement was billed on "
            f"{sorted(recognised)}"
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
