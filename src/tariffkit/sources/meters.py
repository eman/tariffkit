"""Interval readings, without the caller knowing where they came from.

Pricing a cycle needs a meter's import and export registers over a window.
Where those registers are read from -- a Home Assistant recorder, an InfluxDB
series, a Green Button export downloaded or already on disk -- changes how they
are fetched and nothing about what they mean, so it should change nothing in
the code that prices them.

That was not true for a while. The `bill` command carried a branch per source,
each loading its own settings, doing its own window arithmetic, and formatting
its own note, so adding a source meant editing the command that bills, and the
command that bills had opinions about credentials. A reader answers one
question -- readings for this window, and what to call the source in the output
-- and the caller asks it once.

A reader may also *narrow* the window it was given. The utility publishes a day
behind, so an open cycle asked for "through today" really ends at the last
published read; pricing the days beyond it would charge a Base Services Charge
for each and report the shortfall as a gap in the meter. That is the reader's
knowledge, not the biller's, so it travels back in :class:`MeterData`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol

from ..billing import BillingPeriod, IntervalReading

if TYPE_CHECKING:
    from .homeassistant import HaSettings
    from .influx import InfluxSettings
    from .pge import PgeSettings


def _midnight(day: date) -> datetime:
    """Local midnight starting ``day`` -- where a billing cycle boundary falls.

    Wall-clock arithmetic on purpose: a cycle closes at the next local
    midnight, 23 real hours after the previous one across the spring transition
    and 25 across the autumn one.
    """
    from ..timeutil import PACIFIC

    return datetime(day.year, day.month, day.day, tzinfo=PACIFIC)


@dataclass(frozen=True, slots=True)
class MeterData:
    """What a reader answered, and what to call it."""

    readings: list[IntervalReading]
    #: One line naming the source and anything a reader of the output needs to
    #: know about how the readings were derived.
    description: str
    #: Set only when the reader knows the window is narrower than it was asked
    #: for. ``None`` leaves the caller's window alone.
    period: BillingPeriod | None = None


class MeterReader(Protocol):
    """One way of getting interval readings for a window.

    The two attributes are read-only so that a frozen dataclass satisfies this:
    a reader is configuration, and nothing should be rewriting which source it
    is after it has been opened.
    """

    @property
    def name(self) -> str:
        """The ``--source`` spelling that selects this reader."""

    @property
    def needs_period(self) -> bool:
        """False when the readings carry their own window, as a file does."""

    def read(self, period: BillingPeriod | None) -> MeterData: ...


@dataclass(frozen=True, slots=True)
class HomeAssistantMeter:
    """Hourly or five-minute statistics out of the recorder."""

    settings: HaSettings
    resolution: str = "auto"
    name: str = "ha"
    needs_period: bool = True

    def read(self, period: BillingPeriod | None) -> MeterData:
        from .homeassistant import describe_resolution, read_statistics

        assert period is not None
        readings = read_statistics(
            self.settings,
            _midnight(period.start),
            _midnight(period.end) + timedelta(days=1),
            resolution=self.resolution,  # type: ignore[arg-type]
        )
        return MeterData(
            readings,
            f"  source: Home Assistant statistics ({describe_resolution(readings)})",
        )


@dataclass(frozen=True, slots=True)
class InfluxMeter:
    """Raw counter samples, differenced into intervals."""

    settings: InfluxSettings
    minutes: int = 60
    name: str = "influx"
    needs_period: bool = True

    def read(self, period: BillingPeriod | None) -> MeterData:
        from .homeassistant import describe_resolution
        from .influx import read_counters

        assert period is not None
        readings = read_counters(
            self.settings,
            _midnight(period.start),
            _midnight(period.end) + timedelta(days=1),
            timedelta(minutes=self.minutes),
        )
        # Described the way the Home Assistant reader describes itself. The two
        # reported the same interval in different words otherwise -- "744 x
        # 60min" against "744 x hour" -- which reads as the sources disagreeing
        # about something when they do not.
        return MeterData(
            readings,
            f"  source: InfluxDB counters ({describe_resolution(readings)}; "
            f"totals are exact, distribution follows sample density)",
        )


@dataclass(frozen=True, slots=True)
class GreenButtonFile:
    """An export the caller already has, including one on stdin."""

    path: Path | str | IO[str]
    name: str = "green-button"
    needs_period: bool = False

    def read(self, period: BillingPeriod | None) -> MeterData:
        from .greenbutton import read_green_button

        del period
        readings = read_green_button(self.path)
        return MeterData(readings, f"  source: Green Button CSV ({len(readings)} intervals)")


@dataclass(frozen=True, slots=True)
class GreenButtonExport:
    """The utility's own export, downloaded only when the cache cannot answer."""

    settings: PgeSettings | None
    refresh: bool = False
    name: str = "green-button"
    needs_period: bool = True

    def read(self, period: BillingPeriod | None) -> MeterData:
        from .greenbutton import read_green_button
        from .pge import cached_green_button

        assert period is not None
        export = cached_green_button(self.settings, period.start, period.end, refresh=self.refresh)
        # Reported, not applied: whether a short export should shorten the
        # cycle depends on why the window was chosen, which the caller knows.
        narrowed = BillingPeriod(period.start, export.end) if export.end < period.end else None
        readings = read_green_button(export.path)
        origin = "downloaded" if export.downloaded else f"cached {export.covers}"
        return MeterData(
            readings,
            f"  source: Green Button, {origin} "
            f"({len(readings)} intervals, {_short_path(export.path)})",
            narrowed,
        )


def _short_path(path: Path) -> str:
    """A path with the home directory collapsed, for printing."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)
