"""Price metered energy from Home Assistant into a running charge or credit.

The two counters this reads -- grid import and grid export -- are cumulative and
monotonic. They do not reset at midnight, so today's energy is a *difference*
between two points on a counter, not a state anyone can read directly.

Long-term statistics already do that arithmetic, and they do it better than a
listener could. The recorder's ``change`` for an hour is the counter's advance
across it, absorbing counter restarts, integration reloads, and the Rainforest
Eagle's meter-session drops on its own -- the same artefacts
:mod:`tariffkit.sources.homeassistant` exists to work around when it reads a
whole billing cycle back. Statistics, though, only compile at the top of the
hour, so the current hour is read live off the entity state instead: the last
completed hour's recorded ``state`` is a baseline the counter has advanced from.

Pricing is not re-derived here. Hourly readings go to
:class:`tariffkit.billing.BillEngine`, which is the same code that reconciles
against a printed PG&E statement, so a running total and a month-end bill cannot
drift apart in their arithmetic. That buys TOU bucketing, the Energy Commission
Tax, the baseline credit, the Base Services Charge prorated per day, and the
rule that exports before Permission To Operate earn nothing.
"""

from __future__ import annotations

import logging
from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import EnergyConverter

from tariffkit.account import AccountProfile
from tariffkit.billing import Bill, BillingPeriod, IntervalReading, check_coverage
from tariffkit.billing.engine import compute_segments
from tariffkit.errors import TariffKitError
from tariffkit.timeutil import PACIFIC, hour_floor, now_pacific, to_pacific

from .const import (
    CONF_CYCLE_START_DAY,
    CONF_GRID_EXPORT_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    DEFAULT_CYCLE_START_DAY,
)

_LOGGER = logging.getLogger(__name__)

#: The unit the billing engine speaks. Home Assistant's own constant, so a
#: rename upstream is a type error here rather than a silent mismatch.
KWH = UnitOfEnergy.KILO_WATT_HOUR

#: Ceiling on implied power for one hour, in kW. Mirrors
#: ``tariffkit.sources.homeassistant.MAX_INTERVAL_KW``: a statistics series that
#: restarts reports its whole accumulated total as one period's change, which is
#: energy no residential service could have moved.
MAX_INTERVAL_KW = 100.0


@dataclass(frozen=True, slots=True)
class MeterSettings:
    """Which entities carry grid exchange, and when the billing cycle opens."""

    import_entity: str | None = None
    export_entity: str | None = None
    #: Day of the month the meter is read, or 0 for a calendar month.
    cycle_start_day: int = DEFAULT_CYCLE_START_DAY

    @property
    def configured(self) -> bool:
        return bool(self.import_entity or self.export_entity)

    @property
    def entities(self) -> tuple[str, ...]:
        return tuple(e for e in (self.import_entity, self.export_entity) if e)

    @classmethod
    def from_entry(
        cls, values: Mapping[str, Any], profile: AccountProfile | None = None
    ) -> MeterSettings:
        """Read the settings, falling back to a profile's own meter mapping.

        An account profile imported from the CLI already names these two
        entities under ``meter_sources.ha``. Honouring it means importing a
        profile brings its meters with it -- but only until the entities are set
        here, because a key present and empty is a deliberate "no entity" that a
        default must not undo.
        """
        source = profile.meter_sources.home_assistant if profile is not None else None
        raw_import = values.get(CONF_GRID_IMPORT_ENTITY)
        raw_export = values.get(CONF_GRID_EXPORT_ENTITY)
        if CONF_GRID_IMPORT_ENTITY not in values and source is not None:
            raw_import = source.grid_import_entity
        if CONF_GRID_EXPORT_ENTITY not in values and source is not None:
            raw_export = source.grid_export_entity
        try:
            day = int(values.get(CONF_CYCLE_START_DAY, DEFAULT_CYCLE_START_DAY) or 0)
        except TypeError, ValueError:
            day = DEFAULT_CYCLE_START_DAY
        return cls(
            import_entity=raw_import or None,
            export_entity=raw_export or None,
            cycle_start_day=day if 1 <= day <= 31 else DEFAULT_CYCLE_START_DAY,
        )


#: How long after a statement's period ends its evidence still fixes the
#: current cycle's start. A real PG&E cycle runs 27 to 33 days, so a wider gap
#: means at least one statement has been issued that the profile never imported
#: and the next boundary is no longer derivable from what it knows.
#:
#: Measured from the day the derived cycle opened, so the bound is one less than
#: the longest real cycle: at 32 days elapsed the cycle-to-date spans 33 days,
#: and anything beyond that would report a period no bill could have.
STALE_EVIDENCE = timedelta(days=32)


@dataclass(frozen=True, slots=True)
class Cycle:
    """When the current billing cycle opened, and how that was established."""

    start: date
    #: ``statement`` when real evidence fixed it, ``day_of_month`` when the
    #: configured meter-read day was used, ``calendar_month`` when neither was
    #: available. Carried to the entity so a figure that does not match a bill
    #: says why before the reader has to guess.
    source: str


def statement_periods(profile: AccountProfile) -> tuple[BillingPeriod, ...]:
    """Billing periods the profile holds statement evidence for, oldest first.

    One per statement, not one per agreement. A cycle that changed service
    agreement partway -- which is exactly what interconnecting solar does --
    prints two agreement blocks inside one billing period, and it is the period
    the utility bills that a cycle-to-date figure has to follow.
    """
    found: list[BillingPeriod] = []
    for observation in profile.observations:
        spans = [agreement.period for agreement in observation.agreements]
        if not spans:
            continue
        found.append(BillingPeriod(min(s.start for s in spans), max(s.end for s in spans)))
    return tuple(sorted(found, key=lambda period: period.start))


def _by_day_of_month(day: date, start_day: int) -> Cycle:
    """Fall back to a fixed meter-read day, or to the calendar month.

    Months are not all the same length, so a 31st-of-the-month read clamps to
    the 30th in April and the 28th in February rather than failing or skipping
    a cycle.
    """
    if not start_day:
        return Cycle(day.replace(day=1), "calendar_month")
    anchor = min(start_day, monthrange(day.year, day.month)[1])
    if day.day >= anchor:
        return Cycle(day.replace(day=anchor), "day_of_month")
    previous = day.replace(day=1) - timedelta(days=1)
    anchor = min(start_day, monthrange(previous.year, previous.month)[1])
    return Cycle(previous.replace(day=anchor), "day_of_month")


def resolve_cycle(day: date, start_day: int, periods: Sequence[BillingPeriod] = ()) -> Cycle:
    """Find the billing cycle containing ``day``, preferring real evidence.

    A meter-read day is a guess, and a bad one: PG&E reads on business days, so
    a real account's cycles open on the 29th, the 30th, the 1st and the 3rd in
    consecutive months. Any fixed day of the month is therefore wrong for most
    cycles, which is fine for a rough month-to-date figure and not fine for one
    that claims to track a bill.

    Statements say exactly where the boundaries fell, and a profile that has
    imported them knows. Cycles are contiguous -- each period begins the day
    after the last one ended -- so the open cycle's start follows from the most
    recent statement, without waiting for the statement that will close it.

    ``start_day`` remains the fallback, because a profile configured through the
    UI has no statement evidence at all.
    """
    for period in reversed(periods):
        if period.start <= day <= period.end:
            return Cycle(period.start, "statement")
    latest = max((period.end for period in periods), default=None)
    if latest is not None and latest < day:
        opened = latest + timedelta(days=1)
        if day - opened <= STALE_EVIDENCE:
            return Cycle(opened, "statement")
    return _by_day_of_month(day, start_day)


def cycle_start(day: date, start_day: int, periods: Sequence[BillingPeriod] = ()) -> date:
    """The cycle's first day; see :func:`resolve_cycle` for how it is chosen."""
    return resolve_cycle(day, start_day, periods).start


@dataclass(frozen=True, slots=True)
class MeteredUsage:
    """Hourly readings for the current cycle, plus what they do not cover."""

    settings: MeterSettings
    readings: tuple[IntervalReading, ...]
    cycle: BillingPeriod
    today: BillingPeriod
    #: Raw metered totals, before the tariff decides what earns anything.
    #: ``Bill.exported_kwh`` counts only compensated exports, so a pre-PTO site
    #: would otherwise report having exported nothing at all.
    imported_kwh: float
    exported_kwh: float
    imported_today: float
    exported_today: float
    #: How the cycle's start was established: statement evidence, the
    #: configured meter-read day, or the calendar month.
    cycle_source: str = "calendar_month"
    #: Configured entities the recorder had no statistics for.
    missing: tuple[str, ...] = ()
    #: Hours discarded as implausible; their energy is not in the totals.
    dropped: int = 0

    def for_today(self) -> tuple[IntervalReading, ...]:
        return tuple(r for r in self.readings if self.today.contains(r.start))

    def source(self, direction: str) -> str | None:
        """The entity a direction's kWh came from, for an entity's attributes."""
        if direction == "import":
            return self.settings.import_entity
        return self.settings.export_entity


class UsageReader:
    """Assemble hourly grid-exchange readings for the running cycle.

    Completed hours come from the recorder once per hour and are cached until
    the hour rolls over: the coordinator ticks every minute, and re-querying a
    day of statistics sixty times an hour to learn nothing new is the kind of
    polling that makes an integration unwelcome. Only the current hour is
    recomputed each tick, off entity state, which is where all the movement is.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        settings: MeterSettings,
        periods: Sequence[BillingPeriod] = (),
    ) -> None:
        self.hass = hass
        self.settings = settings
        #: Statement evidence, which fixes the cycle boundary far better
        #: than a fixed day of the month can.
        self.periods = tuple(periods)
        self._key: tuple[date, float] | None = None
        # Keyed by epoch seconds, deliberately. Two aware datetimes in one zone
        # compare and hash by wall clock and ignore ``fold`` (PEP 495), so
        # 01:00 PDT and 01:00 PST on a fall-back Sunday are equal keys: the
        # second hour overwrites the first and an hour of metered energy is
        # silently deleted for the rest of the cycle. A float cannot collide.
        self._hours: dict[str, dict[float, float]] = {}
        self._baseline: dict[str, float] = {}
        self._baseline_slot: dict[str, float] = {}
        self._missing: tuple[str, ...] = ()
        #: Hours discarded as implausible. Counted rather than only logged:
        #: a dropped hour is real energy that is not in the total.
        self._dropped = 0
        #: Days a backfill read had to discard an implausible hour on.
        self.discarded: tuple[date, ...] = ()
        #: Configured meters the last backfill read could not fully cover.
        self.absent: tuple[str, ...] = ()

    async def async_usage(self, now: datetime) -> MeteredUsage | None:
        """Readings from the cycle's first midnight through ``now``.

        None when there is nothing to read: no meters configured, or no recorder
        to have recorded them. The recorder is a default integration but it can
        be disabled, and an integration that raised in that case would take the
        rate entities down with it over an optional feature.
        """
        if not self.settings.configured:
            return None
        if "recorder" not in self.hass.config.components:
            return None
        moment = to_pacific(now)
        today = moment.date()
        cycle = resolve_cycle(today, self.settings.cycle_start_day, self.periods)
        hour = hour_floor(moment)
        key = (cycle.start, hour.timestamp())
        if key != self._key:
            await self._async_refresh(cycle.start, hour)
            self._key = key
        return self._assemble(cycle, today, hour, moment)

    async def async_readings(self, opens: date, closes: date) -> list[IntervalReading]:
        """Hourly readings across a closed past window, for backfilling.

        Deliberately separate from the cached path the entities use: that one
        tracks the running cycle and is keyed on the current hour, whereas this
        asks a one-off question about a span that may be months long. Sharing
        the cache would evict the live window on every backfill.

        Only whole completed hours, and no live partial: every hour in a closed
        window has been compiled, so there is nothing to read off entity state.
        """
        opens_at = datetime(opens.year, opens.month, opens.day, tzinfo=PACIFIC)
        closes_at = datetime(closes.year, closes.month, closes.day, tzinfo=PACIFIC) + timedelta(
            days=1
        )
        rows = await self._async_query(opens_at - timedelta(hours=1), closes_at)
        hours: dict[float, list[float]] = {}
        dropped: list[date] = []
        # Per entity, not merged. Two directions summed into one series hide
        # each other: continuous import rows against no export rows at all
        # produce a gapless-looking series of `exported=0`, and every credit the
        # site earned disappears without a gap ever being detected.
        covered: dict[str, set[float]] = {}
        # What the recorder actually held, before this decides what it can use.
        # Kept apart from `covered` so the coverage warning can say which of the
        # two it means: an hour with no row is a hole in the history, an hour
        # whose row was refused is a reading this could not price. Counting both
        # as "missing" sent an owner looking for data loss that is not there.
        recorded: dict[str, set[float]] = {}
        for direction, entity in (
            (0, self.settings.import_entity),
            (1, self.settings.export_entity),
        ):
            if not entity:
                continue
            for row in rows.get(entity) or []:
                slot = float(row["start"])
                if slot < opens_at.timestamp():
                    continue
                recorded.setdefault(entity, set()).add(slot)
                change = row.get("change")
                if change is None:
                    continue
                if change < 0 or change > MAX_INTERVAL_KW:
                    # Loudly, unlike a silent skip: this is a statistics series
                    # catching up after a gap, so real energy is being dropped
                    # and the totals below it will be short by that much.
                    _LOGGER.warning(
                        "Backfill ignoring implausible change of %.1f kWh for %s at %s",
                        change,
                        entity,
                        datetime.fromtimestamp(slot, tz=PACIFIC).isoformat(),
                    )
                    dropped.append(datetime.fromtimestamp(slot, tz=PACIFIC).date())
                    # Deliberately not added to `covered`: the hour is a hole
                    # now, and the hours either side of it carry or lost the
                    # energy it held, which is what `_reconstructed` looks for.
                    continue
                hours.setdefault(slot, [0.0, 0.0])[direction] += change
                covered.setdefault(entity, set()).add(slot)
        self.discarded = tuple(sorted(set(dropped)))
        self.absent = self._absent_series(covered, recorded, opens_at, closes_at)
        reconstructed = self._reconstructed(covered)
        return [
            IntervalReading(
                datetime.fromtimestamp(slot, tz=PACIFIC),
                imported=values[0],
                exported=values[1],
                estimated=slot in reconstructed,
            )
            for slot, values in sorted(hours.items())
        ]

    @staticmethod
    def _reconstructed(covered: Mapping[str, set[float]]) -> set[float]:
        """Hours that carry more than their own energy.

        A counter that was unreachable for a while reports its whole catch-up in
        the first hour the recorder sees again, so that hour's `change` covers
        the outage as well as itself. The kWh total survives -- a cumulative
        counter only depends on its endpoints -- but the shape does not, and the
        shape is what a time-of-use tariff prices.

        :attr:`IntervalReading.estimated` is the library's own word for exactly
        this, and marking it here is what lets everything downstream notice.
        """
        found: set[float] = set()
        for slots in covered.values():
            ordered = sorted(slots)
            for previous, current in pairwise(ordered):
                if current - previous > 3600.0:
                    # Only the hour that *receives* the catch-up. The hour
                    # before a hole has both its own sum and its predecessor's
                    # recorded, so its change is exact and its day is priceable;
                    # refusing it as well cost a second day for every outage and
                    # moved a correctly-priced day's cost into the residual.
                    found.add(current)
        return found

    def _absent_series(
        self,
        covered: Mapping[str, set[float]],
        recorded: Mapping[str, set[float]],
        opens_at: datetime,
        closes_at: datetime,
    ) -> tuple[str, ...]:
        """Say which configured meters the window does not fully account for.

        A meter with no statistics at all is the dangerous case: its direction
        silently reads zero for every hour, so the totals look complete while an
        entire side of the bill is missing.

        The two shortfalls are reported apart because they mean different things
        to whoever reads them. An hour the recorder never wrote is history that
        is gone, and nothing here can recover it. An hour it wrote and this
        refused is a reading that exists and is unusable -- a counter that went
        backwards, or jumped further in one hour than a house can draw -- which
        points at the meter or the integration feeding it, not at the recorder.
        Reporting the second as "missing" sent an owner hunting for data loss
        that had not happened.
        """
        expected = int((closes_at.timestamp() - opens_at.timestamp()) // 3600)
        found: list[str] = []
        for entity in self.settings.entities:
            rows = recorded.get(entity, set())
            if not rows:
                found.append(f"no recorder statistics at all for {entity} over this window")
                continue
            if not expected:
                continue
            missing = expected - len(rows)
            if missing > 0:
                found.append(f"{entity} is missing {missing} of {expected} hour(s) in this window")
            refused = len(rows) - len(covered.get(entity, set()))
            if refused > 0:
                found.append(
                    f"{entity} recorded {refused} of {expected} hour(s) this could not use: "
                    f"a counter that went backwards or advanced by more than "
                    f"{MAX_INTERVAL_KW:.0f} kWh within the hour"
                )
        return tuple(found)

    async def _async_query(
        self, window: datetime, until: datetime
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        """One recorder call for the configured meters over ``window``..``until``."""
        from homeassistant.components.recorder.statistics import statistics_during_period
        from homeassistant.helpers.recorder import get_instance

        try:
            rows = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                window,
                until,
                set(self.settings.entities),
                "hour",
                {"energy": KWH},
                {"change", "state"},
            )
        except Exception as err:
            raise HomeAssistantError(f"recorder could not read statistics: {err}") from err
        return rows

    async def _async_refresh(self, start: date, hour: datetime) -> None:
        """Pull completed hours for the cycle so far out of long-term statistics."""
        from homeassistant.components.recorder.statistics import statistics_during_period
        from homeassistant.helpers.recorder import get_instance

        opens = datetime(start.year, start.month, start.day, tzinfo=PACIFIC)
        # One hour earlier than the cycle, and its rows are dropped below. It is
        # fetched only for its recorded `state`, which is the baseline the live
        # partial hour differences against -- without it the cycle's first hour
        # has nothing to measure from and every entity reads zero until 01:00.
        window = opens - timedelta(hours=1)
        entities = set(self.settings.entities)
        try:
            rows = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                window,
                hour,
                entities,
                "hour",
                {"energy": KWH},
                {"change", "state"},
            )
        except Exception as err:
            # Deliberately broad. The recorder raises SQLAlchemy errors from its
            # own dependency, which this integration does not declare and must
            # not import to name -- recorder owns that pin. Narrowing to a class
            # borrowed transitively would break the day recorder changes engine.
            # An optional feature must not take the rate entities down with it,
            # so anything from the query becomes one recoverable error.
            raise HomeAssistantError(f"recorder could not read statistics: {err}") from err
        self._hours = {}
        self._baseline = {}
        self._baseline_slot = {}
        dropped = 0
        for entity in entities:
            series = rows.get(entity) or []
            hours: dict[float, float] = {}
            for row in series:
                change = row.get("change")
                if change is None:
                    continue
                slot = float(row["start"])
                # A statistics series that restarted reports its whole
                # accumulated total as one hour's change. Dropping it loses that
                # hour's real energy; keeping it would charge for a year of it.
                if slot < opens.timestamp():
                    continue
                if change < 0 or change > MAX_INTERVAL_KW:
                    _LOGGER.warning(
                        "Ignoring implausible change of %.1f kWh for %s at %s",
                        change,
                        entity,
                        datetime.fromtimestamp(slot, tz=PACIFIC).isoformat(),
                    )
                    dropped += 1
                    continue
                hours[slot] = change
            self._hours[entity] = hours
            for row in reversed(series):
                recorded = row.get("state")
                if recorded is not None:
                    self._baseline[entity] = float(recorded)
                    self._baseline_slot[entity] = float(row["start"])
                    break
        self._missing = tuple(sorted(e for e in entities if not self._hours.get(e)))
        self._dropped = dropped

    def _live(self, entity: str) -> float:
        """The counter's advance since the last statistic that recorded a state.

        Zero rather than a guess when the baseline is missing or the counter has
        gone backwards: the next hourly statistic recovers the energy either
        way, and inventing it here would double-count it when that lands.

        Normally the baseline is the hour just ended. When the sensor was down
        for longer it is older, and the advance then covers several hours while
        landing entirely in the current one -- right in total, wrong in shape,
        which is what :attr:`IntervalReading.estimated` is for. The next hourly
        compile redistributes it.
        """
        state = self.hass.states.get(entity)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, None, ""):
            return 0.0
        try:
            value = float(state.state)
        except TypeError, ValueError:
            return 0.0
        unit = state.attributes.get("unit_of_measurement")
        if unit and unit != KWH:
            try:
                value = EnergyConverter.converter_factory(unit, KWH)(value)
            except HomeAssistantError:
                return 0.0
        baseline = self._baseline.get(entity)
        if baseline is None:
            return 0.0
        advance = value - baseline
        if advance <= 0 or advance > MAX_INTERVAL_KW:
            return 0.0
        return advance

    def _assemble(
        self, cycle: Cycle, today: date, hour: datetime, moment: datetime
    ) -> MeteredUsage:
        imports = self._hours.get(self.settings.import_entity or "", {})
        exports = self._hours.get(self.settings.export_entity or "", {})
        readings: list[IntervalReading] = []
        for slot in sorted(set(imports) | set(exports)):
            readings.append(
                IntervalReading(
                    datetime.fromtimestamp(slot, tz=PACIFIC),
                    imported=imports.get(slot, 0.0),
                    exported=exports.get(slot, 0.0),
                )
            )
        grid_import = self.settings.import_entity
        grid_export = self.settings.export_entity
        live_import = self._live(grid_import) if grid_import else 0.0
        live_export = self._live(grid_export) if grid_export else 0.0
        if live_import or live_export:
            # Epoch arithmetic, not wall clock: `hour - timedelta(hours=1)`
            # lands on a non-existent time in the spring-forward gap and on an
            # ambiguous one in autumn, so it misjudges staleness twice a year.
            fresh = hour.timestamp() - 3600.0
            readings.append(
                IntervalReading(
                    hour,
                    imported=live_import,
                    exported=live_export,
                    estimated=any(
                        slot < fresh
                        for entity, slot in self._baseline_slot.items()
                        if entity in {grid_import, grid_export}
                    ),
                    # The partial hour is as long as it has actually been, so a
                    # coverage check reads it as in progress rather than as a
                    # full hour that lost most of its energy.
                    duration=max(moment - hour, timedelta(seconds=1)),
                )
            )
        day = BillingPeriod(today, today)
        return MeteredUsage(
            settings=self.settings,
            readings=tuple(readings),
            cycle=BillingPeriod(cycle.start, today),
            today=day,
            cycle_source=cycle.source,
            imported_kwh=sum(r.imported for r in readings),
            exported_kwh=sum(r.exported for r in readings),
            imported_today=sum(r.imported for r in readings if day.contains(r.start)),
            exported_today=sum(r.exported for r in readings if day.contains(r.start)),
            missing=self._missing,
            dropped=self._dropped,
        )


def coverage_warnings(
    readings: Sequence[IntervalReading],
    period: BillingPeriod,
    through: datetime | None = None,
) -> tuple[str, ...]:
    """The library's own coverage check, told what it is looking at.

    Two facts this caller knows and :func:`tariffkit.billing.check_coverage`
    cannot see. The readings come from a meter's own import and export
    registers, which net at the meter's interval and legitimately leave both
    non-zero once :meth:`UsageReader._assemble` aggregates them to an hour -- on
    a solar site every passing cloud produces one, so reporting it would mark
    every account incomplete forever and train its readers to ignore the
    warnings that matter. And the period may still be running, so it is judged
    as of ``through`` -- the hours that have actually elapsed -- rather than
    against a whole cycle whose remainder has not happened yet.

    Passing the clock rather than suppressing the shortfall is what makes a
    stopped meter visible. Hours that have not arrived and hours that arrived
    empty are indistinguishable in a list of readings; only a clock separates
    them, and without one a meter that died last week goes on quietly reporting
    a smaller number that still calls itself complete.

    An earlier version filtered the library's messages by their text, which is
    the same mistake wearing a disguise: it stopped filtering the moment the
    library grew a warning the filter had not been written for.
    """
    return tuple(
        check_coverage(list(readings), period, netted=True, through=through or now_pacific())
    )


def price(
    profile: AccountProfile, readings: Sequence[IntervalReading], period: BillingPeriod
) -> tuple[Bill | None, str]:
    """Price ``readings`` over ``period``, or say why it could not be priced.

    The engine's own coverage check stays off, because its elapsed-shortfall
    warning is always true of a running total and says nothing. The problems it
    would also have caught -- gaps, overlaps, reconstructed intervals -- are
    real, and :func:`coverage_warnings` reports those separately rather than
    letting them go with the noise. The engine's *pricing* warnings (an
    uncovered tax vintage, a stale CCA card, exports before Permission To
    Operate) come through here as always.

    Refusing is the honest answer when the account history does not reach back to
    the start of the period, which a cycle that opened before the profile's first
    epoch really does. Pricing the days it *does* cover would return a smaller
    number that looks complete -- fewer days of Base Services Charge and none of
    the energy -- and this codebase would rather return nothing than something
    plausible and wrong. But nothing has to say why, or the entity is an
    unexplained ``unknown``, so the reason travels with the refusal.
    """
    try:
        return compute_segments(profile.segments_for(period), readings, check=False), ""
    except (TariffKitError, ValueError) as err:
        _LOGGER.debug("Cannot price %s to %s: %s", period.start, period.end, err)
        return None, f"cannot price {period.start} to {period.end}: {err}"
