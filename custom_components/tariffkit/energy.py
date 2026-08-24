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
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import EnergyConverter

from tariffkit.account import AccountProfile
from tariffkit.billing import Bill, BillingPeriod, IntervalReading
from tariffkit.billing.engine import Segment, compute_segments
from tariffkit.errors import TariffKitError
from tariffkit.timeutil import PACIFIC, hour_floor, to_pacific

from .const import (
    CONF_CYCLE_START_DAY,
    CONF_GRID_EXPORT_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    DEFAULT_CYCLE_START_DAY,
)

_LOGGER = logging.getLogger(__name__)

#: Home Assistant's own energy units, and the one the billing engine speaks.
KWH = "kWh"

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


def cycle_start(day: date, start_day: int) -> date:
    """First day of the billing cycle containing ``day``.

    ``start_day`` is the day of the month the meter is read. Months are not all
    the same length, so a 31st-of-the-month read clamps to the 30th in April and
    the 28th in February rather than failing or skipping a cycle.
    """
    if not start_day:
        return day.replace(day=1)
    anchor = min(start_day, monthrange(day.year, day.month)[1])
    if day.day >= anchor:
        return day.replace(day=anchor)
    previous = day.replace(day=1) - timedelta(days=1)
    return previous.replace(day=min(start_day, monthrange(previous.year, previous.month)[1]))


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
    #: Configured entities the recorder had no statistics for.
    missing: tuple[str, ...] = ()

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

    def __init__(self, hass: HomeAssistant, settings: MeterSettings) -> None:
        self.hass = hass
        self.settings = settings
        self._key: tuple[date, datetime] | None = None
        self._hours: dict[str, dict[datetime, float]] = {}
        self._baseline: dict[str, float] = {}
        self._baseline_slot: dict[str, datetime] = {}
        self._missing: tuple[str, ...] = ()

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
        start = cycle_start(today, self.settings.cycle_start_day)
        hour = hour_floor(moment)
        key = (start, hour)
        if key != self._key:
            await self._async_refresh(start, hour)
            self._key = key
        return self._assemble(start, today, hour, moment)

    async def _async_refresh(self, start: date, hour: datetime) -> None:
        """Pull completed hours for the cycle so far out of long-term statistics."""
        from homeassistant.components.recorder.statistics import statistics_during_period
        from homeassistant.helpers.recorder import get_instance
        from sqlalchemy.exc import SQLAlchemyError

        opens = datetime(start.year, start.month, start.day, tzinfo=PACIFIC)
        entities = set(self.settings.entities)
        try:
            rows = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                opens,
                hour,
                entities,
                "hour",
                {"energy": KWH},
                {"change", "state"},
            )
        except SQLAlchemyError as err:
            raise HomeAssistantError(f"recorder could not read statistics: {err}") from err
        self._hours = {}
        self._baseline = {}
        self._baseline_slot = {}
        for entity in entities:
            series = rows.get(entity) or []
            hours: dict[datetime, float] = {}
            for row in series:
                change = row.get("change")
                if change is None:
                    continue
                slot = datetime.fromtimestamp(row["start"], tz=PACIFIC)
                # A statistics series that restarted reports its whole
                # accumulated total as one hour's change. Dropping it loses that
                # hour's real energy; keeping it would charge for a year of it.
                if change < 0 or change > MAX_INTERVAL_KW:
                    _LOGGER.warning(
                        "Ignoring implausible change of %.1f kWh for %s at %s",
                        change,
                        entity,
                        slot.isoformat(),
                    )
                    continue
                hours[slot] = change
            self._hours[entity] = hours
            for row in reversed(series):
                recorded = row.get("state")
                if recorded is not None:
                    self._baseline[entity] = float(recorded)
                    self._baseline_slot[entity] = datetime.fromtimestamp(row["start"], tz=PACIFIC)
                    break
        self._missing = tuple(sorted(e for e in entities if not rows.get(e)))

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
        if state is None or state.state in ("unknown", "unavailable", None, ""):
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

    def _assemble(self, start: date, today: date, hour: datetime, moment: datetime) -> MeteredUsage:
        imports = self._hours.get(self.settings.import_entity or "", {})
        exports = self._hours.get(self.settings.export_entity or "", {})
        readings: list[IntervalReading] = []
        for slot in sorted(set(imports) | set(exports)):
            readings.append(
                IntervalReading(
                    slot,
                    imported=imports.get(slot, 0.0),
                    exported=exports.get(slot, 0.0),
                )
            )
        grid_import = self.settings.import_entity
        grid_export = self.settings.export_entity
        live_import = self._live(grid_import) if grid_import else 0.0
        live_export = self._live(grid_export) if grid_export else 0.0
        if live_import or live_export:
            fresh = hour - timedelta(hours=1)
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
            cycle=BillingPeriod(start, today),
            today=day,
            imported_kwh=sum(r.imported for r in readings),
            exported_kwh=sum(r.exported for r in readings),
            imported_today=sum(r.imported for r in readings if day.contains(r.start)),
            exported_today=sum(r.exported for r in readings if day.contains(r.start)),
            missing=self._missing,
        )


def segments(profile: AccountProfile, period: BillingPeriod) -> list[Segment]:
    """Split ``period`` wherever the account's own history changes epoch.

    A cycle that crosses a tariff change is two blocks on one statement, not a
    blended rate, and :func:`tariffkit.billing.engine.compute_segments` prices it
    that way. A day almost never spans a change; a month-to-date total does
    whenever the account changed schedule.
    """
    bounds = [d for d in profile.effective_dates if period.start < d <= period.end]
    starts = [period.start, *bounds]
    ends = [d - timedelta(days=1) for d in bounds] + [period.end]
    return [
        Segment(
            config=profile.config_at(datetime(s.year, s.month, s.day, 12, tzinfo=PACIFIC)),
            period=BillingPeriod(s, e),
        )
        for s, e in zip(starts, ends, strict=True)
    ]


def price(
    profile: AccountProfile, readings: Sequence[IntervalReading], period: BillingPeriod
) -> Bill | None:
    """Price ``readings`` over ``period``, or None if no epoch governs it.

    Coverage checking is off: a running total is always missing the rest of the
    day, and warning about that every minute says nothing. The engine's *pricing*
    warnings -- an uncovered tax vintage, a stale CCA card, exports before
    Permission To Operate -- still come through, because those are real.
    """
    try:
        return compute_segments(segments(profile, period), readings, check=False)
    except (TariffKitError, ValueError) as err:
        _LOGGER.debug("Cannot price %s to %s: %s", period.start, period.end, err)
        return None
