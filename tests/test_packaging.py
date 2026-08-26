"""Distribution metadata and integration-boundary checks."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import tariffkit

ROOT = Path(__file__).parent.parent


def test_project_identity_and_version_are_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    manifest = json.loads(
        (ROOT / "custom_components" / "tariffkit" / "manifest.json").read_text(encoding="utf-8")
    )
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    lock_text = (ROOT / "uv.lock").read_text(encoding="utf-8")
    lock = tomllib.loads(lock_text)

    assert project["name"] == "tariffkit"
    assert project["license"] == "MIT"
    assert project["requires-python"] == ">=3.14.2"
    assert lock["requires-python"] == project["requires-python"]
    assert "python_full_version < '3.14.2'" not in lock_text
    assert not any(
        package["name"] == "homeassistant" and package["version"].startswith("2026.2.")
        for package in lock["package"]
    )
    assert manifest["domain"] == "tariffkit"
    assert manifest["version"] == project["version"] == tariffkit.__version__
    assert manifest["requirements"] == [f"tariffkit=={project['version']}"]
    assert hacs["zip_release"] is True
    assert hacs["filename"] == "tariffkit.zip"
    assert hacs["hide_default_branch"] is True
    assert hacs["country"] == "US"
    # 2026.3.0 is the floor because it is the first release on Python 3.14,
    # which the integration's PEP 758 `except` groups need to parse at all.
    assert hacs["homeassistant"] == "2026.3.0"


def test_maintainer_dependencies_are_not_public_extras() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "dev" not in config["project"]["optional-dependencies"]
    assert "regen" not in config["project"]["optional-dependencies"]
    assert "security" not in config["project"]["optional-dependencies"]
    assert "regen" in config["dependency-groups"]
    assert config["dependency-groups"]["security"] == ["pip-audit==2.10.1"]


def test_home_assistant_does_not_vendor_the_library() -> None:
    component = ROOT / "custom_components" / "tariffkit"
    source = (component / "__init__.py").read_text(encoding="utf-8")

    assert not (component / "vendored").exists()
    assert "sys.path" not in source
    assert (component / "brand" / "icon.png").is_file()
