"""Readings reach the biller without it knowing where they came from."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from tariffkit.billing import BillingPeriod, IntervalReading
from tariffkit.sources.meters import (
    GreenButtonExport,
    GreenButtonFile,
    HomeAssistantMeter,
    InfluxMeter,
    MeterData,
    MeterReader,
)
from tariffkit.timeutil import PACIFIC


@dataclass(frozen=True, slots=True)
class StubMeter:
    """A source the library has never heard of."""

    name: str = "stub"
    needs_period: bool = True

    def read(self, period: BillingPeriod | None) -> MeterData:
        assert period is not None
        start = datetime(period.start.year, period.start.month, period.start.day, tzinfo=PACIFIC)
        return MeterData(
            [IntervalReading(start, imported=1.0, duration=timedelta(hours=1))],
            "  source: something else entirely",
        )


def test_a_reader_the_library_does_not_ship_still_satisfies_the_protocol() -> None:
    """The point of the protocol: adding a source touches no billing code.

    Every branch used to live in the `bill` command, so a new source meant
    editing the command that prices bills -- and that command had opinions
    about credentials, windows, and how each source describes itself.
    """
    reader: MeterReader = StubMeter()
    data = reader.read(BillingPeriod(date(2026, 7, 1), date(2026, 7, 1)))

    assert isinstance(data, MeterData)
    assert len(data.readings) == 1
    assert data.period is None


def test_every_shipped_reader_satisfies_the_protocol() -> None:
    readers: list[MeterReader] = [
        HomeAssistantMeter(settings=None),  # type: ignore[arg-type]
        InfluxMeter(settings=None),  # type: ignore[arg-type]
        GreenButtonFile(path="x.csv"),
        GreenButtonExport(settings=None),
    ]
    assert [reader.name for reader in readers] == [
        "ha",
        "influx",
        "green-button",
        "green-button",
    ]
    # Only a file carries its own window; everything else is asked for one.
    assert [reader.needs_period for reader in readers] == [True, True, False, True]


def test_narrowing_is_reported_not_applied() -> None:
    """A reader says the window was short; whether that matters is the caller's.

    The utility publishes a day behind, so an open cycle really ends at the
    last published read -- but explicit dates are an answer, not a guess to
    refine, and only the caller knows which it asked with.
    """
    narrowed = MeterData([], "", BillingPeriod(date(2026, 8, 1), date(2026, 8, 30)))
    assert narrowed.period == BillingPeriod(date(2026, 8, 1), date(2026, 8, 30))
    assert MeterData([], "").period is None
