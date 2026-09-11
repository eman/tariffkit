"""Every source is optional, and the tools say what that leaves working."""

from __future__ import annotations

from pathlib import Path

import pytest

from tariffkit.cli.availability import first_available_meter_source, survey
from tariffkit.cli.commands import main

METER_ENV = (
    "HA_HOST",
    "HA_TOKEN",
    "INFLUXDB3_HOST",
    "INFLUXDB3_DATABASE",
    "INFLUXDB3_AUTH_TOKEN",
    "PGE_USERNAME",
    "PGE_PASSWORD",
    "TARIFFKIT_HA_IMPORT_ENTITY",
    "TARIFFKIT_HA_EXPORT_ENTITY",
    "TARIFFKIT_INFLUX_IMPORT_ENTITY",
    "TARIFFKIT_INFLUX_EXPORT_ENTITY",
)


@pytest.fixture
def bare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for variable in METER_ENV:
        monkeypatch.delenv(variable, raising=False)
    # The Green Button cache is a source too, and it is found through
    # XDG_CACHE_HOME, which the shared config isolation does not cover. Without
    # this a developer with a real cache sees a different survey than CI does.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.mark.usefixtures("bare")
def test_rates_answer_with_nothing_configured() -> None:
    """The one source that never depends on configuration.

    Rate data ships in the wheel, so "what does a kilowatt-hour cost right now"
    is answerable on a machine with no login, no meter, and no network.
    """
    rates = next(status for status in survey() if status.name == "rates")
    assert rates.available is True
    assert rates.remedy == ""
    assert "now" in rates.features


@pytest.mark.usefixtures("bare")
def test_nothing_configured_means_no_meter_source_and_a_remedy_each() -> None:
    statuses = survey()
    assert first_available_meter_source(statuses) is None
    for status in statuses:
        if status.name == "rates":
            continue
        assert status.available is False
        # Every unavailable source says what would turn it on. An audit that
        # only reported False would leave the reader where the old errors did.
        assert status.remedy


@pytest.mark.usefixtures("bare")
def test_a_utility_login_is_not_needed_for_a_meter_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The portal is one source among several, not a precondition for any.

    An account reading its own meter should never be asked for a utility
    password, and the survey is what lets `bill` pick without asking.
    """
    monkeypatch.setenv("HA_HOST", "http://ha.example")
    monkeypatch.setenv("HA_TOKEN", "tok")
    monkeypatch.setenv("TARIFFKIT_HA_IMPORT_ENTITY", "sensor.in")
    monkeypatch.setenv("TARIFFKIT_HA_EXPORT_ENTITY", "sensor.out")

    statuses = survey()
    by_name = {status.name: status for status in statuses}
    assert by_name["home_assistant"].available is True
    assert by_name["pge_portal"].available is False
    assert first_available_meter_source(statuses) == "ha"


@pytest.mark.usefixtures("bare")
def test_half_configured_is_unavailable_not_half_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host with no entities cannot answer, and says which half is missing."""
    monkeypatch.setenv("HA_HOST", "http://ha.example")
    monkeypatch.setenv("HA_TOKEN", "tok")

    status = next(s for s in survey() if s.name == "home_assistant")
    assert status.available is False
    assert "grid counters" in status.remedy


@pytest.mark.usefixtures("bare")
def test_a_cached_export_is_a_source_without_a_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Downloading an export needs the portal; reading one back does not.

    Requiring a login to price intervals already on disk would mean an account
    that had fetched a year of them could not price any of it offline.
    """
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    held = cache / "tariffkit" / "pge" / "green-button"
    held.mkdir(parents=True)

    assert first_available_meter_source(survey()) is None

    (held / "2026-08-01_2026-08-31.csv").write_text("", encoding="utf-8")
    statuses = survey()
    assert next(s for s in statuses if s.name == "green_button_cache").available is True
    assert first_available_meter_source(statuses) == "green-button"


@pytest.mark.usefixtures("bare")
def test_the_sources_command_reports_what_works(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sources"]) == 0
    out = capsys.readouterr().out
    assert "yes  rates" in out
    assert "no   pge_portal" in out
    assert "no meter source to read" in out
