"""Every source is optional, and the tools say what that leaves working."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tariffkit.cli.availability import (
    choose_meter_source,
    first_available_meter_source,
    survey,
)
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
        # Rates always answer; the account is not a meter source and is checked
        # by its own test.
        if status.name in {"rates", "account"}:
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


@pytest.mark.usefixtures("bare")
def test_choosing_a_source_stops_at_the_first_one_that_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing probes in preference order; it does not survey everything.

    Every probe loads a settings object, and loading one consults the keyring.
    On a macOS Keychain that is an access record -- potentially a prompt --
    against a utility password, on a machine that only ever reads Home
    Assistant. Measured out of band, this took `bill` from three keyring
    lookups to none; asserted here is the mechanism, which is what can be
    pinned without reaching into how each source module bound `get_secret`.
    """
    monkeypatch.setenv("HA_HOST", "http://ha.example")
    monkeypatch.setenv("HA_TOKEN", "tok")
    monkeypatch.setenv("TARIFFKIT_HA_IMPORT_ENTITY", "sensor.in")
    monkeypatch.setenv("TARIFFKIT_HA_EXPORT_ENTITY", "sensor.out")

    chosen, looked = choose_meter_source()
    assert chosen == "ha"
    assert [status.name for status in looked] == ["home_assistant"]

    # Nothing is lost when it has to look further: a run with no meter source
    # reports every source it tried, which is what the error message lists.
    for variable in ("HA_HOST", "HA_TOKEN"):
        monkeypatch.delenv(variable)
    chosen, looked = choose_meter_source()
    assert chosen is None
    assert [status.name for status in looked] == [
        "home_assistant",
        "influxdb",
        "pge_portal",
        "green_button_cache",
    ]

    # And the full survey still asks everything -- that is what it is for.
    assert len(survey()) == 6


@pytest.mark.usefixtures("bare")
def test_the_cache_remedy_does_not_print_a_home_directory() -> None:
    """These lines get pasted into bug reports; an absolute path carries a name."""
    status = next(s for s in survey() if s.name == "green_button_cache")
    assert status.available is False
    assert str(Path.home()) not in status.remedy


@pytest.mark.usefixtures("bare")
def test_the_account_is_listed_as_a_prerequisite() -> None:
    """Most commands need an account; listing only meters left that off the page.

    Without one there is no tariff history, no statement evidence, and nothing
    that knows when the current cycle began -- so `bill` needs explicit dates
    and `account periods` has nothing to record against.
    """
    status = next(s for s in survey() if s.name == "account")
    assert status.available is False
    assert "account init" in status.remedy
    assert "bill without --start/--end" in status.features


@pytest.mark.usefixtures("bare")
def test_an_existing_account_reports_available() -> None:
    from tariffkit.account import AccountEpoch, AccountProfile
    from tariffkit.cli.account_store import AccountStore
    from tariffkit.config import Config

    # The same default directory `survey` resolves, not a guess at its layout.
    store = AccountStore()
    store.save(AccountProfile((AccountEpoch(date(2025, 1, 1), Config()),), name="home"))

    status = next(s for s in survey() if s.name == "account")
    assert status.available is True
    assert status.remedy == ""


@pytest.mark.usefixtures("bare")
def test_the_cache_only_counts_files_bill_could_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sources` must agree with what `bill` would actually select.

    A glob for `*.csv` counted names the export cache deliberately skips, so
    the survey reported a Green Button source available while `bill` could not
    use the file and still wanted a login.
    """
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    held = cache / "tariffkit" / "pge" / "green-button"
    held.mkdir(parents=True)

    # A csv that is not an export range.
    (held / "notes.csv").write_text("", encoding="utf-8")
    status = next(s for s in survey() if s.name == "green_button_cache")
    assert status.available is False

    # One that is.
    (held / "2026-08-01_2026-08-31.csv").write_text("", encoding="utf-8")
    status = next(s for s in survey() if s.name == "green_button_cache")
    assert status.available is True
