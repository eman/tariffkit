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

    assert project["name"] == "tariffkit"
    assert project["license"] == "MIT"
    assert manifest["domain"] == "tariffkit"
    assert manifest["version"] == project["version"] == tariffkit.__version__
    assert manifest["requirements"] == [f"tariffkit=={project['version']}"]


def test_maintainer_dependencies_are_not_public_extras() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "dev" not in config["project"]["optional-dependencies"]
    assert "regen" not in config["project"]["optional-dependencies"]
    assert "regen" in config["dependency-groups"]


def test_home_assistant_does_not_vendor_the_library() -> None:
    component = ROOT / "custom_components" / "tariffkit"
    source = (component / "__init__.py").read_text(encoding="utf-8")

    assert not (component / "vendored").exists()
    assert "sys.path" not in source
    assert (component / "brand" / "icon.png").is_file()
