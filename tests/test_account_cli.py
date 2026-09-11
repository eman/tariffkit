"""Focused tests for named-account CLI maintenance."""

from __future__ import annotations

import argparse
import importlib
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from tariffkit.account import (
    AccountEpoch,
    AccountObservation,
    AccountProfile,
    MeterSource,
    MeterSources,
    ObservedAgreement,
)
from tariffkit.billing import BillingPeriod, IntervalReading
from tariffkit.cli.account_commands import (
    _statement_reason,
    migrate_existing,
    sync_profile,
)
from tariffkit.cli.account_store import AccountStore
from tariffkit.cli.commands import _known_periods, _mqtt_settings, build_parser, main
from tariffkit.config import Config
from tariffkit.errors import ConfigError
from tariffkit.models import Supplier


def observation(*, tariff: str, digest: str) -> AccountObservation:
    return AccountObservation(
        agreements=(
            ObservedAgreement(
                provider="pge",
                statement_date=date(2026, 2, 1),
                period=BillingPeriod(date(2026, 1, 1), date(2026, 1, 31)),
                tariff=tariff,
                supplier=Supplier.BUNDLED,
                source_digest=digest,
            ),
        ),
        source_digest=digest,
    )


def test_account_migration_never_probes_repository_audit_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "account.toml").write_text("[[history]]\neffective = 2025-01-01\n", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text('tariff = "EV2-A"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    profile = migrate_existing(config_path=config)

    assert profile.epochs[0].config.tariff == "EV2-A"


def test_account_migration_requires_explicit_existing_audit_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="legacy audit account file not found"):
        migrate_existing(audit_path=tmp_path / "missing.toml")


def test_account_init_update_and_export_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "config.toml"
    config.write_text(
        'tariff = "E-ELEC"\ninterconnection_year = 2026\npto_date = "2026-06-03"\n',
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--config",
                str(config),
                "account",
                "init",
                "--effective",
                "2025-01-01",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "account",
                "update",
                "--effective",
                "2026-01-01",
                "--tariff",
                "EV2-A",
                "--apply",
            ]
        )
        == 0
    )

    exported = tmp_path / "profile.json"
    assert main(["account", "export", "--output", str(exported)]) == 0
    payload = json.loads(exported.read_text(encoding="utf-8"))
    assert [epoch["config"]["tariff"] for epoch in payload["epochs"]] == ["E-ELEC", "EV2-A"]
    assert "amount_due" not in exported.read_text(encoding="utf-8")


def _init_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, effective: str) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "config.toml"
    config.write_text(
        'tariff = "E-ELEC"\ninterconnection_year = 2026\npto_date = "2026-06-03"\n',
        encoding="utf-8",
    )
    assert main(["--config", str(config), "account", "init", "--effective", effective]) == 0


def _account_with_statement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AccountProfile:
    """An account whose last statement closed on 2026-08-27."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    store = AccountStore(tmp_path)
    profile = AccountProfile(
        (AccountEpoch(date(2025, 1, 1), Config()),),
        observations=(
            AccountObservation(
                agreements=(
                    ObservedAgreement(
                        provider="pge",
                        statement_date=date(2026, 9, 1),
                        period=BillingPeriod(date(2026, 7, 29), date(2026, 8, 27)),
                        tariff="E-ELEC",
                    ),
                ),
            ),
        ),
    )
    store.save(profile)
    return profile


@freeze_time("2026-09-09T12:00:00-07:00")
def test_bill_without_dates_prices_the_open_cycle_from_statement_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "What do I owe so far?" was the one question it could not answer alone."""
    _account_with_statement(tmp_path, monkeypatch)
    monkeypatch.setenv("PGE_USERNAME", "person@example.invalid")
    monkeypatch.setenv("PGE_PASSWORD", "secret")
    monkeypatch.setattr("tariffkit.sources.cached_bill_periods", lambda *a, **k: [])
    asked: dict[str, date] = {}

    def fake(settings: object, start: date, end: date, **kwargs: object) -> object:
        asked.update(start=start, end=end)
        return SimpleNamespace(
            path=_export_csv(tmp_path), start=start, end=end, downloaded=False, covers="cached"
        )

    monkeypatch.setattr("tariffkit.sources.pge.cached_green_button", fake)

    assert main(["bill", "--source", "green-button"]) == 0

    # The cycle after the last statement: contiguous, so it opened on the 28th.
    assert asked == {"start": date(2026, 8, 28), "end": date(2026, 9, 9)}
    out = capsys.readouterr().out
    assert "cycle: 2026-08-28 to 2026-09-09, the boundary your statements print" in out


@freeze_time("2026-09-09T12:00:00-07:00")
def test_bill_without_dates_takes_the_boundary_the_utility_billed_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No statement imported, and still exact: the portal lists what it billed."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    AccountStore(tmp_path).save(AccountProfile((AccountEpoch(date(2025, 1, 1), Config()),)))
    _stub_export(
        tmp_path,
        monkeypatch,
        portal_periods=[
            BillingPeriod(date(2026, 6, 30), date(2026, 7, 28)),
            BillingPeriod(date(2026, 7, 29), date(2026, 8, 27)),
        ],
    )

    assert main(["bill", "--source", "green-button"]) == 0

    out = capsys.readouterr().out
    # Cycles are contiguous, so the open one began the day after the last close.
    assert "cycle: 2026-08-28 to 2026-09-09, the boundary PG&E billed on" in out


@freeze_time("2026-09-09T12:00:00-07:00")
def test_bill_without_dates_says_when_the_boundary_is_a_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A calendar month will not match a bill, so it must not read as one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    AccountStore(tmp_path).save(AccountProfile((AccountEpoch(date(2025, 1, 1), Config()),)))
    _stub_export(tmp_path, monkeypatch)

    assert main(["bill", "--source", "green-button"]) == 0

    out = capsys.readouterr().out
    assert "cycle: 2026-09-01 to 2026-09-09, a calendar month, which is a guess" in out
    assert "store PG&E credentials" in out


@freeze_time("2026-09-09T12:00:00-07:00")
def test_bill_without_dates_uses_a_configured_meter_read_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "tariffkit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tariffkit" / "config.toml").write_text(
        "[billing]\ncycle_start_day = 29\n", encoding="utf-8"
    )
    AccountStore(tmp_path).save(AccountProfile((AccountEpoch(date(2025, 1, 1), Config()),)))
    _stub_export(tmp_path, monkeypatch)

    assert main(["bill", "--source", "green-button"]) == 0

    assert "cycle: 2026-08-29 to 2026-09-09" in capsys.readouterr().out


def test_bill_refuses_one_date_without_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Half a window is a typo, not a request to guess the other half."""
    _account_with_statement(tmp_path, monkeypatch)

    assert main(["bill", "--start", "2026-08-01"]) == 1

    assert "give both --start and --end" in capsys.readouterr().err


def test_bill_without_an_account_still_asks_for_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing says when the cycle began, so it does not invent one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert main(["bill", "--source", "green-button"]) == 1

    assert "there is nothing to say when the current billing cycle began" in (
        capsys.readouterr().err
    )


def _stub_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    portal_periods: list[BillingPeriod] | None = None,
) -> None:
    """Credentials that satisfy `PgeSettings.load`, and an export to read.

    The portal's own cycle boundaries are stubbed too, and empty by default:
    every one of these asserts which basis was used, so a real lookup would
    make the answer depend on the machine.
    """
    monkeypatch.setenv("PGE_USERNAME", "person@example.invalid")
    monkeypatch.setenv("PGE_PASSWORD", "secret")
    monkeypatch.setattr(
        "tariffkit.sources.cached_bill_periods", lambda *a, **k: portal_periods or []
    )
    monkeypatch.setattr(
        "tariffkit.sources.pge.cached_green_button",
        lambda settings, start, end, **k: SimpleNamespace(
            path=_export_csv(tmp_path),
            start=start,
            end=end,
            downloaded=False,
            covers="cached",
        ),
    )


def _export_csv(tmp_path: Path) -> Path:
    path = tmp_path / "export.csv"
    path.write_text(
        "start,imported,exported\n"
        "2026-09-01T02:00:00-07:00,1.5,0\n"
        "2026-09-01T03:00:00-07:00,0,2.5\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "message",
    [
        "statement-0007.pdf OCR did not produce a self-checking statement",
        "statement-0007.pdf could not be read as a PDF",
        "statement-0007.pdf has no text layer, so it is a scan or a print-to-PDF export",
        "statement-0007.pdf produced no pages to recognise",
        "statement-0007.pdf failed its self-check (3 problem(s))",
        "statement-0007.pdf OCR read the statement but it did not check out (2): a: b; c",
    ],
)
def test_a_skipped_statement_never_names_the_file_the_sync_deleted(message: str) -> None:
    """The source is a loop index inside a cache directory the run removes.

    Stripping everything up to the first `": "` instead of the name itself
    left it in four of these, and threw away the explanatory half of the
    fifth, keeping only its problem list.
    """
    from tariffkit.providers.pge.statements.errors import StatementError

    reason = _statement_reason(StatementError(message), Path("/tmp/cache/statement-0007.pdf"))

    assert "statement-0007" not in reason
    assert reason and not reason.startswith(":")
    if "did not check out" in message:
        assert reason.startswith("OCR read the statement")


def test_portal_periods_do_not_overrule_a_statement_that_covers_the_same_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The portal lists a bill per agreement; a statement is one page.

    Taking its list wholesale opened a cycle split by interconnection on the
    day the second agreement began, disagreeing with the statement and with
    what the integration reports for the same account. Exercised through
    `_known_periods`, because that is where the list was taken wholesale.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("PGE_USERNAME", "person@example.invalid")
    monkeypatch.setenv("PGE_PASSWORD", "secret")
    monkeypatch.setattr(
        "tariffkit.sources.cached_bill_periods",
        lambda *a, **k: [
            BillingPeriod(date(2026, 6, 1), date(2026, 6, 2)),
            BillingPeriod(date(2026, 6, 3), date(2026, 6, 29)),
            BillingPeriod(date(2026, 6, 30), date(2026, 7, 28)),
        ],
    )
    profile = AccountProfile(
        (AccountEpoch(date(2025, 1, 1), Config()),),
        observations=(
            AccountObservation(
                agreements=(
                    ObservedAgreement(
                        provider="pge",
                        statement_date=date(2026, 7, 3),
                        period=BillingPeriod(date(2026, 6, 1), date(2026, 6, 29)),
                        tariff="E-ELEC",
                    ),
                ),
            ),
        ),
    )
    args = build_parser().parse_args(["bill", "--source", "green-button"])

    origins = _known_periods(args, profile)

    assert [(p.start, p.end, origin) for p, origin in origins.items()] == [
        (date(2026, 6, 1), date(2026, 6, 29), "statement"),
        (date(2026, 6, 30), date(2026, 7, 28), "portal"),
    ]


def test_a_period_the_merge_dropped_does_not_come_back_with_a_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Labelling as the merges went along kept what the merges had discarded.

    A partial statement nested inside a whole cycle is superseded by it, and
    putting it back beside the winner handed `resolve_cycle` the partial span
    again -- undoing the wider-period rule inside the CLI.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("tariffkit.sources.cached_bill_periods", lambda *a, **k: [])
    partial = BillingPeriod(date(2026, 6, 10), date(2026, 6, 15))
    whole = BillingPeriod(date(2026, 6, 1), date(2026, 6, 29))
    profile = AccountProfile(
        (AccountEpoch(date(2025, 1, 1), Config()),),
        observations=(
            AccountObservation(
                agreements=(
                    ObservedAgreement(
                        provider="pge",
                        statement_date=date(2026, 6, 20),
                        period=partial,
                        tariff="E-ELEC",
                    ),
                ),
            ),
        ),
        billing_periods=(whole,),
    )

    origins = _known_periods(
        build_parser().parse_args(["bill", "--source", "green-button"]), profile
    )

    assert list(origins) == [whole]
    assert origins[whole] == "recorded"


@freeze_time("2026-09-09T12:00:00-07:00")
def test_the_basis_names_the_source_that_actually_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One old statement plus a live portal is not "the boundary your statements print".

    Deciding the label from "does the account hold any statements" said exactly
    that, for a cycle no statement had anything to do with.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    store = AccountStore(tmp_path)
    store.save(
        AccountProfile(
            (AccountEpoch(date(2025, 1, 1), Config()),),
            observations=(
                AccountObservation(
                    agreements=(
                        ObservedAgreement(
                            provider="pge",
                            statement_date=date(2026, 2, 3),
                            period=BillingPeriod(date(2026, 1, 1), date(2026, 1, 30)),
                            tariff="E-ELEC",
                        ),
                    ),
                ),
            ),
        )
    )
    _stub_export(
        tmp_path,
        monkeypatch,
        portal_periods=[BillingPeriod(date(2026, 7, 29), date(2026, 8, 27))],
    )
    capsys.readouterr()

    assert main(["bill", "--source", "green-button"]) == 0

    out = capsys.readouterr().out
    assert "cycle: 2026-08-28 to 2026-09-09, the boundary PG&E billed on" in out
    assert "your statements print" not in out


def test_naming_a_config_file_never_reaches_for_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--config` says which file to price from; consulting the account is a
    write to the configuration directory it has no business making."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "elsewhere.toml"
    config.write_text('tariff = "E-ELEC"\ninterconnection_year = 2026\n', encoding="utf-8")

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("the account was consulted despite --config")

    monkeypatch.setattr("tariffkit.cli.commands._account_store", refuse)

    assert main(["--config", str(config), "now"]) == 0
    assert not (tmp_path / "tariffkit").exists()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # Nothing is configured, so there is nothing to default to. The answer
        # names every source that would work rather than the first one tried.
        (["bill"], "no meter source is configured"),
        (
            ["bill", "--start", "2026-08-01", "--end", "2026-08-10"],
            "no meter source is configured",
        ),
        (["bill", "readings.csv"], "could not read"),
        # Asking for one by name still asks that one, and it still says what is
        # missing for it in particular.
        (["bill", "--source", "influx"], "InfluxDB"),
        (["bill", "--source", "green-button"], "needs a utility login"),
    ],
)
def test_bill_without_any_source_names_every_source_that_would_work(
    argv: list[str],
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unconfigured account is told its options, not the first one tried.

    The default used to be the constant "ha", so an account pricing from
    InfluxDB or from an export it had already downloaded was told that HA_TOKEN
    was not set -- naming the one source it had not set up.

    Asserted through what each invocation actually goes and asks for -- naming
    the source in the failure it produces -- because the same test written
    against `parse_args` passed with the resolution reverted.
    """
    _account_with_statement(tmp_path, monkeypatch)
    for variable in ("HA_HOST", "HA_TOKEN", "INFLUXDB3_HOST", "PGE_USERNAME", "PGE_PASSWORD"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr("tariffkit.sources.cached_bill_periods", lambda *a, **k: [])

    assert main(argv) == 1

    assert expected in capsys.readouterr().err


def test_naming_entities_on_the_command_line_chooses_that_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--ha-import-entity` is a choice of source, not just a value.

    The survey only reads the config file and the profile, so a run that named
    its counters as flags reported Home Assistant unconfigured -- and then
    either priced from a portal download instead, or refused while telling the
    user to name the very counters they had just named.
    """
    from tariffkit.cli.meters import _default_meter_source

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for variable in ("HA_HOST", "HA_TOKEN", "INFLUXDB3_HOST", "PGE_USERNAME", "PGE_PASSWORD"):
        monkeypatch.delenv(variable, raising=False)

    def args(**overrides: object) -> argparse.Namespace:
        base = {
            "csv": None,
            "config": None,
            "ha_import_entity": None,
            "ha_export_entity": None,
            "influx_import_entity": None,
            "influx_export_entity": None,
        }
        return argparse.Namespace(**{**base, **overrides})

    assert (
        _default_meter_source(
            args(ha_import_entity="sensor.in", ha_export_entity="sensor.out"), None
        )
        == "ha"
    )
    assert (
        _default_meter_source(args(influx_import_entity="in", influx_export_entity="out"), None)
        == "influx"
    )
    # One half is not a choice: it cannot read a direction it was not given.
    with pytest.raises(ConfigError):
        _default_meter_source(args(ha_import_entity="sensor.in"), None)


def test_home_assistant_is_still_preferred_when_more_than_one_source_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The account's own meter, not the utility's export.

    PG&E's export was missing thirty days of one cycle where the meter matched
    the statement to 0.00 kWh, so the order matters and is not alphabetical.
    Configuring everything must still reach for Home Assistant first.
    """
    _account_with_statement(tmp_path, monkeypatch)
    monkeypatch.setenv("HA_HOST", "http://ha.invalid")
    monkeypatch.setenv("HA_TOKEN", "tok")
    monkeypatch.setenv("INFLUXDB3_HOST", "influx.invalid")
    monkeypatch.setenv("INFLUXDB3_DATABASE", "db")
    monkeypatch.setenv("INFLUXDB3_AUTH_TOKEN", "tok")
    monkeypatch.setenv("PGE_USERNAME", "u")
    monkeypatch.setenv("PGE_PASSWORD", "p")
    monkeypatch.setenv("TARIFFKIT_HA_IMPORT_ENTITY", "sensor.in")
    monkeypatch.setenv("TARIFFKIT_HA_EXPORT_ENTITY", "sensor.out")
    monkeypatch.setattr("tariffkit.sources.cached_bill_periods", lambda *a, **k: [])

    from tariffkit.cli.meters import _default_meter_source

    # The flags `bill` always supplies. Naming a pair on the command line is
    # itself a choice of source, so they have to be absent for this to be
    # testing the preference order.
    args = argparse.Namespace(
        csv=None,
        config=None,
        ha_import_entity=None,
        ha_export_entity=None,
        influx_import_entity=None,
        influx_export_entity=None,
    )
    assert _default_meter_source(args, None) == "ha"


def test_a_csv_path_with_another_source_still_resolves_a_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--source ha` ignores the CSV, so it still needs a period, not an assertion."""
    _account_with_statement(tmp_path, monkeypatch)
    csv = _export_csv(tmp_path)

    # It fails on the missing Home Assistant host, which is a configuration
    # error and reported as one -- not on an assertion about the window.
    code = main(["bill", str(csv), "--source", "ha"])

    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_account_periods_records_the_boundaries_for_anything_reading_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the CLI has portal credentials; the account is how they travel."""
    _init_account(tmp_path, monkeypatch, effective="2025-01-01")
    monkeypatch.setenv("PGE_USERNAME", "person@example.invalid")
    monkeypatch.setenv("PGE_PASSWORD", "secret")
    monkeypatch.setattr(
        "tariffkit.sources.cached_bill_periods",
        lambda *a, **k: [
            BillingPeriod(date(2026, 6, 30), date(2026, 7, 28)),
            BillingPeriod(date(2026, 7, 29), date(2026, 8, 27)),
        ],
    )
    capsys.readouterr()

    assert main(["account", "periods"]) == 0
    assert "preview only" in capsys.readouterr().out
    assert AccountStore(tmp_path).load().billing_periods == ()

    assert main(["account", "periods", "--apply"]) == 0

    stored = AccountStore(tmp_path).load().billing_periods
    assert [(p.start, p.end) for p in stored] == [
        (date(2026, 6, 30), date(2026, 7, 28)),
        (date(2026, 7, 29), date(2026, 8, 27)),
    ]
    # And they leave with the export, which is the integration's import format.
    capsys.readouterr()
    assert main(["account", "export"]) == 0
    assert json.loads(capsys.readouterr().out)["billing_periods"][-1] == {
        "start": "2026-07-29",
        "end": "2026-08-27",
    }


def test_account_show_answers_what_is_in_force_not_what_history_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`show` and `history` were the same output when no evidence was recorded."""
    _init_account(tmp_path, monkeypatch, effective="2025-01-01")
    assert (
        main(["account", "update", "--effective", "2026-01-01", "--tariff", "EV2-A", "--apply"])
        == 0
    )
    capsys.readouterr()

    assert main(["account", "show"]) == 0
    shown = capsys.readouterr().out
    assert main(["account", "history"]) == 0
    history = capsys.readouterr().out

    assert shown != history
    # The settings themselves, resolved to one moment -- not the epoch list.
    assert "in force since 2026-01-01" in shown
    assert "tariff" in shown and "EV2-A" in shown
    assert "acc_plus_segment" in shown
    assert "2025-01-01" not in shown
    # The timeline, which is history's job.
    assert "epochs" in history and "2025-01-01" in history


def test_account_show_json_carries_the_resolved_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_account(tmp_path, monkeypatch, effective="2025-01-01")
    capsys.readouterr()

    assert main(["account", "show", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["effective"] == "2025-01-01"
    assert payload["config"]["tariff"] == "E-ELEC"
    assert payload["epochs"] == 1
    assert payload["observations"] == 0


def test_account_show_says_so_when_every_epoch_is_still_in_the_future(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An account imported ahead of a move-in date has nothing in force."""
    _init_account(tmp_path, monkeypatch, effective="2099-01-01")
    capsys.readouterr()

    assert main(["account", "show"]) == 0

    out = capsys.readouterr().out
    assert "nothing in force yet" in out
    assert "2099-01-01" in out


def test_account_update_json_names_the_account_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The key is `account`: there is one, and it is not selected by name."""
    config = tmp_path / "config.toml"
    config.write_text(
        'tariff = "E-ELEC"\ninterconnection_year = 2026\npto_date = "2026-06-03"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert main(["--config", str(config), "account", "init", "--effective", "2025-01-01"]) == 0
    capsys.readouterr()

    assert (
        main(["account", "update", "--effective", "2026-01-01", "--tariff", "EV2-A", "--json"]) == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert [epoch["config"]["tariff"] for epoch in payload["account"]["epochs"]] == [
        "E-ELEC",
        "EV2-A",
    ]


def test_account_update_previews_without_writing(tmp_path: Path) -> None:
    monkeypatch_config = tmp_path / "config.toml"
    monkeypatch_config.write_text(
        'tariff = "E-ELEC"\ninterconnection_year = 2026\npto_date = "2026-06-03"\n',
        encoding="utf-8",
    )
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert (
            main(
                [
                    "--config",
                    str(monkeypatch_config),
                    "account",
                    "init",
                    "--effective",
                    "2025-01-01",
                ]
            )
            == 0
        )
        assert (
            main(
                [
                    "account",
                    "update",
                    "--effective",
                    "2026-01-01",
                    "--tariff",
                    "EV2-A",
                ]
            )
            == 0
        )
        assert [epoch.effective for epoch in AccountStore(tmp_path).load().epochs] == [
            date(2025, 1, 1)
        ]
    finally:
        monkeypatch.undo()


def test_account_source_preview_apply_and_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "config.toml"
    config.write_text(
        'tariff = "E-ELEC"\ninterconnection_year = 2026\npto_date = "2026-06-03"\n',
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--config",
                str(config),
                "account",
                "init",
                "--effective",
                "2025-01-01",
            ]
        )
        == 0
    )
    capsys.readouterr()
    store = AccountStore(tmp_path)
    assert store.load().meter_sources == MeterSources()

    assert (
        main(
            [
                "account",
                "source",
                "set",
                "ha",
                "--grid-import-entity",
                "sensor.grid_in",
                "--grid-export-entity",
                "sensor.grid_out",
                "--json",
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["applied"] is False
    assert store.load().meter_sources == MeterSources()

    assert (
        main(
            [
                "account",
                "source",
                "set",
                "ha",
                "--grid-import-entity",
                "sensor.grid_in",
                "--grid-export-entity",
                "sensor.grid_out",
                "--apply",
            ]
        )
        == 0
    )
    assert store.load().meter_sources.ha == MeterSource("sensor.grid_in", "sensor.grid_out")
    capsys.readouterr()
    assert main(["account", "source", "show", "ha", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["configured"] is True
    assert shown["grid_import_entity"] == "sensor.grid_in"


@pytest.mark.parametrize("source", ["ha", "influx"])
def test_bill_passes_profile_entities_and_cli_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    profile = AccountProfile(
        (AccountEpoch(date(2025, 1, 1), Config()),),
        meter_sources=MeterSources(
            ha=MeterSource("sensor.profile_in", "sensor.profile_out"),
            influx=MeterSource("profile_in", "profile_out"),
        ),
    )
    store = AccountStore(tmp_path)
    store.save(profile)

    captured: dict[str, object] = {}
    import tariffkit.sources as sources

    class FakeSettings:
        @classmethod
        def load(cls, **kwargs: object) -> object:
            captured.update(kwargs)
            return object()

    def readings(
        _settings: object, start: object, end: object, *args: object, **kwargs: object
    ) -> list[IntervalReading]:
        assert isinstance(start, datetime)
        assert isinstance(end, datetime)
        return [IntervalReading(start, imported=1.0, duration=end - start)]

    monkeypatch.setattr(
        sources,
        "HaSettings" if source == "ha" else "InfluxSettings",
        FakeSettings,
    )
    # Patched where the reader looks it up, not on the package namespace: the
    # readers import from their own module, so `tariffkit.sources.X` is a name
    # nothing reads at call time.
    monkeypatch.setattr(
        "tariffkit.sources.homeassistant.read_statistics"
        if source == "ha"
        else "tariffkit.sources.influx.read_counters",
        readings,
    )

    flags = (
        ["--ha-import-entity", "sensor.cli_in", "--ha-export-entity", "sensor.cli_out"]
        if source == "ha"
        else ["--influx-import-entity", "cli_in", "--influx-export-entity", "cli_out"]
    )
    assert (
        main(
            [
                "bill",
                "--source",
                source,
                "--start",
                "2026-07-01",
                "--end",
                "2026-07-01",
                *flags,
                "--json",
            ]
        )
        == 0
    )
    assert captured["profile_source"] == (
        profile.meter_sources.ha if source == "ha" else profile.meter_sources.influx
    )
    assert captured["import_entity"] == ("sensor.cli_in" if source == "ha" else "cli_in")
    assert captured["export_entity"] == ("sensor.cli_out" if source == "ha" else "cli_out")


def test_account_import_statement_previews_then_applies(
    tmp_path: Path, monkeypatch: object
) -> None:
    store = AccountStore(tmp_path)
    store.save(
        AccountProfile((AccountEpoch(date(2025, 1, 1), Config()),)),
    )
    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"%PDF synthetic")
    imported = observation(tariff="EV2-A", digest="a" * 64)

    reconcile_module = importlib.import_module("tariffkit.providers.pge.reconcile")

    monkeypatch.setattr(reconcile_module, "import_statement", lambda _path: imported)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert main(["account", "import-statement", str(pdf), "--json"]) == 0
    assert len(store.load().epochs) == 1
    assert main(["account", "import-statement", str(pdf), "--apply"]) == 0
    assert [epoch.config.tariff for epoch in store.load().epochs] == [
        "E-ELEC",
        "EV2-A",
    ]


def test_account_sync_removes_private_cache_after_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AccountStore(tmp_path)
    store.save(
        AccountProfile((AccountEpoch(date(2025, 1, 1), Config()),)),
    )
    imported = observation(tariff="EV2-A", digest="b" * 64)
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))

    class Session:
        def __init__(self) -> None:
            self.signed_in = False

        def __enter__(self) -> Session:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def login(self, *, force: bool = False) -> None:
            self.signed_in = True

        def bill_history(self) -> list[dict[str, str]]:
            # The portal lists nothing for a session that never minted a CSRF
            # token, which is what a resumed session is until `login()` runs.
            if not self.signed_in:
                return []
            return [{"billId": "bill-1", "billDate": "2026-02-01"}]

        def download_bill(self, _bill_id: str) -> bytes:
            return b"%PDF synthetic"

    import tariffkit.sources.pge as pge_module

    opened: list[Session] = []

    def _session(_settings: object) -> Session:
        opened.append(Session())
        return opened[-1]

    monkeypatch.setattr(pge_module, "PgeSession", _session)
    monkeypatch.setattr(pge_module.PgeSettings, "load", lambda _path=None: object())
    reconcile_module = importlib.import_module("tariffkit.providers.pge.reconcile")
    monkeypatch.setattr(reconcile_module, "import_statement", lambda _path: imported)

    _profile, proposals, skipped = sync_profile(store, apply=False)

    assert len(proposals) == 1
    assert skipped == []
    assert not tuple(cache.rglob("*.pdf"))
    # Without this the statement list comes back empty and the sync reports
    # "0 statement update(s)" against an account that has plenty.
    assert opened[0].signed_in, "sync must sign in before listing statements"


def test_one_unreadable_statement_does_not_discard_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The portal lists what it lists; the parser does not have to like all of it.

    A `StatementError` from one document used to propagate out of the whole
    loop, so an account with statements going back years imported none of them
    because the newest one would not parse -- and the command exited non-zero,
    as though the portal or the credentials were at fault. Reported from a real
    sync as `error: statement-0000.pdf: no total amount due found`, which named
    a temporary file inside a cache directory the function deletes on its way
    out: nothing the owner could open, and no clue which statement it meant.
    """
    from tariffkit.providers.pge.statements.errors import StatementError

    store = AccountStore(tmp_path)
    store.save(AccountProfile((AccountEpoch(date(2025, 1, 1), Config()),)))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    class Session:
        def __enter__(self) -> Session:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def login(self, *, force: bool = False) -> None:
            return None

        def bill_history(self) -> list[dict[str, str]]:
            return [
                {"billId": "newest", "billDate": "2026-09-04"},
                {"billId": "older", "billDate": "2026-08-04"},
            ]

        def download_bill(self, bill_id: str) -> bytes:
            return bill_id.encode()

    import tariffkit.sources.pge as pge_module

    monkeypatch.setattr(pge_module, "PgeSession", lambda _settings: Session())
    monkeypatch.setattr(pge_module.PgeSettings, "load", lambda _path=None: object())

    good = observation(tariff="EV2-A", digest="c" * 64)

    def _import(path: Path) -> object:
        if path.read_bytes() == b"newest":
            raise StatementError(f"{path.name}: no total amount due found")
        return good

    reconcile_module = importlib.import_module("tariffkit.providers.pge.reconcile")
    monkeypatch.setattr(reconcile_module, "import_statement", _import)

    _profile, proposals, skipped = sync_profile(store, apply=False)

    assert len(proposals) == 1, "the readable statement still imported"
    assert len(skipped) == 1
    # Named by the date the utility issued it, not by the temporary file the
    # loop index produced -- that name is gone by the time anyone reads it.
    assert skipped[0]["statement"] == "2026-09-04"
    assert skipped[0]["reason"] == "no total amount due found"
    assert "statement-0000" not in skipped[0]["reason"]


def test_mqtt_cli_accepts_insecure_auth_escape_hatch() -> None:
    args = build_parser().parse_args(
        ["mqtt", "--broker", "broker.local", "--username", "user", "--allow-insecure-auth"]
    )

    settings = _mqtt_settings(args, from_account=False)

    assert settings.allow_insecure_auth is True
