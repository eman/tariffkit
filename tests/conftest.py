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


@pytest.fixture(autouse=True, scope="session")
def recorder_migration_annotations() -> None:
    """Make the recorder's deferred type names resolvable at runtime.

    ``pytest-homeassistant-custom-component`` patches
    ``migration._find_schema_errors`` with ``autospec=True``, which asks
    :func:`inspect.signature` to evaluate its annotations. That module imports
    ``Recorder`` only under ``TYPE_CHECKING``, and on Python 3.14 the evaluation
    is no longer lazy enough to tolerate it -- so the recorder fixture raises
    ``NameError`` before any test of ours runs. Binding the real class is a
    fixture for the harness, not for the integration.
    """
    from homeassistant.components.recorder import migration
    from homeassistant.components.recorder.core import Recorder
    from homeassistant.helpers import recorder as recorder_helper
    from sqlalchemy.orm.session import Session

    migration.Recorder = Recorder
    recorder_helper.Session = Session
    migration.Session = Session
