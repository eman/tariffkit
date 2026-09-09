"""Shared test setup."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import pytest


@dataclass
class CapturedLogs:
    """Records emitted onto one logger, with the message actually formatted.

    Stands in for pytest's ``caplog``, which is unreachable in this suite:
    ``pytest_homeassistant_custom_component`` overrides that fixture by
    requesting it, and pytest 9 reads a plugin fixture that asks for its own
    name as a recursive dependency and errors at collection. Every test
    touching ``caplog`` was therefore uncollectable -- including, as it happens,
    the one guarding the plausibility ceiling on meter readings.

    Named for what it captures rather than shadowing ``caplog``, because
    shadowing it hits the same recursion. Formats each record through the
    logging machinery rather than storing ``record.msg``, so an assertion sees
    the message a reader of the log would see, %-substitutions and all.
    """

    records: list[logging.LogRecord] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(record.getMessage() for record in self.records)

    def at(self, level: int) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno == level]


#: What the ``captured_logs`` fixture hands a test: call it with a logger (or a
#: logger name) and get back the records emitted onto it for the rest of the test.
type CaptureLogs = Callable[[logging.Logger | str], CapturedLogs]


@pytest.fixture
def captured_logs() -> Iterator[CaptureLogs]:
    """Attach a capturing handler to a named logger for one test."""
    attached: list[tuple[logging.Logger, logging.Handler, int, bool]] = []

    def capture(logger: logging.Logger | str) -> CapturedLogs:
        target = logging.getLogger(logger) if isinstance(logger, str) else logger
        captured = CapturedLogs()

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.records.append(record)

        handler = _Handler()
        attached.append((target, handler, target.level, target.propagate))
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
        return captured

    yield capture
    for target, handler, level, propagate in attached:
        target.removeHandler(handler)
        target.setLevel(level)
        target.propagate = propagate


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
def recorder_migration_annotations() -> Iterator[None]:
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

    patched = (
        (migration, "Recorder", Recorder),
        (migration, "Session", Session),
        (recorder_helper, "Session", Session),
    )
    for module, name, value in patched:
        setattr(module, name, value)
    yield
    # Undo it: these are third-party modules shared with every other test in the
    # run, and a fixture that mutates them permanently is one more thing that
    # can explain a confusing failure somewhere else.
    for module, name, _ in patched:
        with suppress(AttributeError):
            delattr(module, name)
