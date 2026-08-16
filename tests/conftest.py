"""Shared test setup."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_user_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the developer's real config out of the tests.

    ``Config.load`` reads ~/.config/tariffkit/config.toml and TARIFFKIT_*, so
    without this a machine configured for a CCA fails tests that assert bundled
    defaults -- and CI and local disagree.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("config")))
    for name in [key for key in os.environ if key.startswith("TARIFFKIT_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TARIFFKIT_DISABLE_KEYRING", "1")
    # Guard against Config falling back to a real home directory.
    monkeypatch.setattr(Path, "home", lambda: Path(os.environ["XDG_CONFIG_HOME"]))
