"""`doctor` must agree with `run` about what a run needs."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from audit import preflight
from tariffkit.account import AccountEpoch, AccountProfile, MeterSource, MeterSources
from tariffkit.config import Config


def test_an_account_id_is_not_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without one the portal lists the login's own account, as `run` does.

    Demanding it reported a run that works as one that cannot start.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("PGE_USERNAME", "someone@example.invalid")
    monkeypatch.setenv("PGE_PASSWORD", "secret")
    monkeypatch.setenv("PGE_BROWSER_COOKIE", "b")
    monkeypatch.setenv("PGE_VALIDATION_COOKIE", "v")
    monkeypatch.delenv("PGE_ACCOUNT_ID", raising=False)
    # Nothing else is looked up in the operating-system keyring.
    monkeypatch.setattr("tariffkit.sources.pge.get_secret", lambda name: None)

    check = preflight._credentials()
    assert check.ok, check.detail
    assert "login's own account" in check.detail


def test_the_influx_series_are_read_from_the_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Where `run` reads them; the config file no longer has to name them."""
    for name in ("INFLUXDB3_HOST", "INFLUXDB3_DATABASE", "INFLUXDB3_AUTH_TOKEN"):
        monkeypatch.setenv(name, "placeholder")
    for name in ("TARIFFKIT_INFLUX_IMPORT_ENTITY", "TARIFFKIT_INFLUX_EXPORT_ENTITY"):
        monkeypatch.delenv(name, raising=False)
    profile = AccountProfile(
        (AccountEpoch(date(2025, 1, 1), Config()),),
        meter_sources=MeterSources(influx=MeterSource("grid_in", "grid_out")),
    )
    monkeypatch.setattr(preflight, "_load_profile", lambda: profile)
    seen: list[Any] = []

    def read(settings: Any, start: object, end: object) -> list[object]:
        seen.append(settings)
        return [object()]

    monkeypatch.setattr("tariffkit.sources.influx.read_counters", read)

    check = preflight._influx()
    assert check.ok, check.detail
    assert (seen[0].import_entity, seen[0].export_entity) == ("grid_in", "grid_out")


def test_the_ocr_hint_names_the_package_manager_the_machine_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not apt for every machine that is not a Mac: Fedora has dnf."""
    import shutil
    import sys

    from tariffkit.providers.pge.statements import ocr

    monkeypatch.setattr(ocr, "available", lambda: False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/dnf" if name == "dnf" else None)

    check = preflight._recognition()
    assert "dnf install tesseract poppler-utils" in check.detail
    assert "apt" not in check.detail
