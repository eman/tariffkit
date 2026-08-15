"""Pricing a statement from its own metered quantities.

The question behind every mismatch is which side is wrong, and it has exactly
two answers: the rates, or the energy they were applied to. Telling them apart
took a hand-run script and a long afternoon on each of the four cycles that
needed it, which is three afternoons too many for a question this mechanical.

So it is asked automatically. A statement prints the kilowatt-hours it billed,
broken out by time-of-use period, alongside the charges. Pricing *those*
quantities with *our* rates removes the meter from the question entirely:

* the line then reconciles -- the rates are right and our interval data
  disagrees with the meter about which hours the energy arrived in
* it still does not -- the rates, the vintage, or the map is wrong, and no
  amount of better metering will fix it

On the four cycles that led to this, the first answer was right every time:
2026-03 moved from +0.184 to -0.027 and 2026-06 from +0.109 to +0.001.

Deliberately not a repair. Nothing here feeds back into the computed bill --
substituting the statement's own numbers into the bill being checked against it
would make the harness agree with anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

from tariffkit.config import Config
from tariffkit.engine import RateEngine
from tariffkit.models import TouPeriod
from tariffkit.timeutil import PACIFIC

from ..statements.model import Section, Statement

#: Printed row labels that name a time-of-use period. The utility qualifies
#: some with a season -- "Off Peak Summer" on the generation page -- which is
#: redundant here, because the season follows from the date the row was billed
#: on and is resolved from the tariff rather than from the wording.
TOU_LABELS: dict[str, TouPeriod] = {
    "peak": TouPeriod.PEAK,
    "part peak": TouPeriod.PART_PEAK,
    "off peak": TouPeriod.OFF_PEAK,
}

#: The sub-heading the Solar Billing Plan prints export rows under. Once solar
#: is interconnected the delivery page carries the same three time-of-use labels
#: twice -- once for energy produced and once for energy delivered -- and they
#: are opposite signs of the meter. Reading both as import doubles the
#: reconstruction and turns the verdict on every solar mismatch upside down.
EXPORT_BLOCK = "energy produced"


def _tou(label: str) -> tuple[TouPeriod, str | None] | None:
    """The period a printed label names, and the season if it names one."""
    folded = " ".join(label.lower().replace("-", " ").split())
    season = None
    for candidate in ("summer", "winter"):
        if folded.endswith(f" {candidate}"):
            folded = folded.removesuffix(f" {candidate}")
            season = candidate
    period = TOU_LABELS.get(folded)
    return (period, season) if period is not None else None


def _moment_in(
    rates: RateEngine,
    days: Sequence[date],
    period: TouPeriod,
    season: str | None,
) -> datetime | None:
    """An instant the tariff prices at ``period``, and at ``season`` if given.

    Found by asking the tariff rather than by hardcoding 4-9pm, so a schedule
    with different hours -- or a vintage that moved them -- needs no change here.

    Several days are offered because a cycle can span the season boundary, and
    the provider's page prints one row per season. Pricing a winter row on a
    summer day is a rate error dressed as a reconciliation failure.
    """
    for day in days:
        for hour in range(24):
            moment = datetime.combine(day, time(hour), PACIFIC)
            if rates.tariff.period(moment) is not period:
                continue
            if season is not None and str(rates.tariff.season(moment)) != season:
                continue
            return moment
    return None


def _days(statement: Statement) -> list[date]:
    span = (statement.period.end - statement.period.start).days
    return [statement.period.start + timedelta(days=offset) for offset in range(span + 1)]


def priced_from_statement(statement: Statement, config: Config) -> dict[str, float]:
    """Per-kWh components, priced from the quantities the statement itself printed.

    Empty when the statement prints no metered rows to work from, which is not
    a failure -- some sections carry only charges.
    """
    rates = RateEngine(config)
    totals: dict[str, float] = {}

    # One section only. The delivery detail's time-of-use rows carry the full
    # retail rate, generation included, and the provider's page prints the same
    # energy again at its generation rate -- pricing both counts every
    # kilowatt-hour twice, which showed up as a generation line exactly double
    # the printed one. The provider's page is the fallback for a statement that
    # has no delivery detail rather than an addition to it.
    for section in (
        statement.section(Section.PGE_DELIVERY),
        statement.section(Section.CCA_GENERATION),
    ):
        if section is None:
            continue
        before = len(totals)
        for line in section.charged:
            # Export rows carry the same labels as import rows and must not be
            # added to an import reconstruction.
            if line.block.strip().lower() == EXPORT_BLOCK:
                continue
            found = _tou(line.label)
            if found is None or not line.kwh:
                continue
            period, season = found
            # The sub-period a row was billed under, when the utility split the
            # cycle at a rate change -- which vintage applied is the whole point
            # of that split. Otherwise any day of the cycle whose season matches
            # the row, since the provider's page names the season rather than
            # the dates.
            days = [line.subperiod[0]] if line.subperiod else _days(statement)
            moment = _moment_in(rates, days, period, season)
            if moment is None:
                continue
            price = rates.tariff.price_at(moment)
            for name, value in price.components.items():
                totals[name] = totals.get(name, 0.0) + value * line.kwh
        if len(totals) > before:
            break

    return totals


def fixed_from_statement(statement: Statement) -> dict[str, float]:
    """Charges the statement prints outright rather than per kilowatt-hour.

    Read rather than recomputed. The Base Services Charge is a daily amount and
    has reconciled to the cent on every cycle, so re-deriving it here would only
    add a way to be wrong about something already agreed.

    Keys carry the side they belong to, matching how a rule names them, because
    the same word means different things on different sides.
    """
    out: dict[str, float] = {}
    for section in statement.sections:
        for line in section.charged:
            label = line.label.strip().lower()
            if label == "base services charge":
                out["fixed:base_services_charge"] = (
                    out.get("fixed:base_services_charge", 0.0) + line.amount
                )
            # Credits the statement says it applied this cycle, keyed to match
            # the side a rule names them under. Read, not recomputed: what was
            # applied is a ledger outcome, and the statement is the record of it.
            elif label == "energy export credits applied":
                key = (
                    "applied:generation"
                    if section.name is Section.CCA_GENERATION
                    else "applied:delivery"
                )
                out[key] = out.get(key, 0.0) + line.amount
            elif label == "energy export bonus credits applied":
                key = (
                    "applied:generation"
                    if section.name is Section.CCA_GENERATION
                    else "applied:bonus"
                )
                out[key] = out.get(key, 0.0) + line.amount
    return out
