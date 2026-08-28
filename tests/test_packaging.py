"""Distribution metadata and integration-boundary checks."""

from __future__ import annotations

import json
import struct
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
    # Not a coincidence: 2026.3.0 is the first Home Assistant release on the
    # 3.14.2 patch line the floor above declares, so the two move together and
    # raising either one raises the other. See docs/packaging_strategy.md.
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


def test_brand_assets_match_the_home_assistant_spec() -> None:
    """HACS requires brand/icon.png; the brands CDN spec fixes the sizes.

    Home Assistant only ever serves PNGs out of brand/ (see the ALLOWED_IMAGES
    allowlist in homeassistant.components.brands), so anything else in there is
    dead weight shipped to every install.
    """
    brand = ROOT / "custom_components" / "tariffkit" / "brand"
    expected = {"icon.png": 256, "icon@2x.png": 512}

    assert sorted(path.name for path in brand.iterdir()) == sorted(expected)
    for name, side in expected.items():
        header = (brand / name).read_bytes()[:26]
        assert header[12:16] == b"IHDR", f"{name} is not a PNG"
        width, height, _depth, color_type = struct.unpack(">IIBB", header[16:26])
        assert (width, height) == (side, side)
        assert color_type == 6, f"{name} must keep its alpha channel"
