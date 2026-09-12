"""Readings reach the biller without it knowing where they came from."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_narrowing_is_reported_not_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A short export reports the window it really covers; applying it is the caller's.

    The utility publishes a day behind, so an open cycle asked for "through
    today" really ends at the last published read -- pricing the days past it
    would charge a Base Services Charge for each and call the shortfall a gap
    in the meter. Explicit dates are an answer, not a guess to refine, so the
    reader says what it covered and `bill` decides.
    """
    export = tmp_path / "export.csv"
    export.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "tariffkit.sources.pge.cached_green_button",
        lambda settings, start, end, **kw: SimpleNamespace(
            path=export, end=date(2026, 8, 30), downloaded=False, covers="cached"
        ),
    )
    monkeypatch.setattr("tariffkit.sources.greenbutton.read_green_button", lambda *a, **k: [])

    asked = BillingPeriod(date(2026, 8, 1), date(2026, 9, 1))
    data = GreenButtonExport(settings=None).read(asked)
    assert data.period == BillingPeriod(date(2026, 8, 1), date(2026, 8, 30))

    # An export reaching the end of the window narrows nothing.
    monkeypatch.setattr(
        "tariffkit.sources.pge.cached_green_button",
        lambda settings, start, end, **kw: SimpleNamespace(
            path=export, end=date(2026, 9, 1), downloaded=False, covers="cached"
        ),
    )
    assert GreenButtonExport(settings=None).read(asked).period is None
