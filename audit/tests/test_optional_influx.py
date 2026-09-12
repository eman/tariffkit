"""InfluxDB is a comparison on the statistics path, not a prerequisite."""

from __future__ import annotations

from datetime import date

import pytest

from audit.cli import optional_influx
from tariffkit.account import AccountEpoch, AccountProfile, MeterSource, MeterSources
from tariffkit.config import Config
from tariffkit.errors import TariffKitError


def _profile(influx: MeterSource | None = None) -> AccountProfile:
    return AccountProfile(
        (AccountEpoch(date(2025, 1, 1), Config()),),
        meter_sources=MeterSources(influx=influx),
    )


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host, database and token present -- the case that used to slip through."""
    for name in ("INFLUXDB3_HOST", "INFLUXDB3_DATABASE", "INFLUXDB3_AUTH_TOKEN"):
        monkeypatch.setenv(name, "placeholder")
    for name in ("TARIFFKIT_INFLUX_IMPORT_ENTITY", "TARIFFKIT_INFLUX_EXPORT_ENTITY"):
        monkeypatch.delenv(name, raising=False)


def test_credentials_without_series_is_skipped_on_the_statistics_path() -> None:
    """`load` validates host/database/token, not the series names.

    So credentials present and series absent returned an object and the read
    raised anyway -- the shape the first attempt at this missed, which still
    failed `--readings statistics` for a Home Assistant-only account.
    """
    assert optional_influx(_profile(), "statistics") is None


def test_the_same_configuration_still_fails_when_influx_is_what_was_asked_for() -> None:
    with pytest.raises(TariffKitError, match="import_entity and export_entity not set"):
        optional_influx(_profile(), "influx")


def test_a_configured_profile_is_used_on_both_paths() -> None:
    profile = _profile(MeterSource("grid_in", "grid_out"))
    for readings_from in ("influx", "statistics"):
        settings = optional_influx(profile, readings_from)
        assert settings is not None
        assert settings.require_entities() == ("grid_in", "grid_out")
