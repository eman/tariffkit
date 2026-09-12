"""Distribution metadata and integration-boundary checks."""

from __future__ import annotations

import ast
import importlib
import itertools
import json
import re
import struct
import tomllib
from pathlib import Path

import pytest
from homeassistant.helpers import device_registry as dr

import tariffkit

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"


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
    # The floor is set by the newest Home Assistant API the integration calls,
    # not by the Python patch line -- it was 2026.3.0 on that older reasoning,
    # which said nothing about whether anything ran there. HACS refuses the
    # update below this version, which is the only thing standing between a
    # 2026.7 user and an AttributeError. See docs/packaging_strategy.md.
    assert hacs["homeassistant"] == "2026.8.0"
    # The reason for that number, asserted rather than remembered: 2026.8.0 is
    # the first release carrying the call `coordinator.py` makes.
    assert hasattr(dr.DeviceRegistry, "async_get_device_by_identifier")


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


def test_the_integration_imports_only_what_the_library_exports() -> None:
    """Every `from tariffkit...` name in the integration must resolve.

    The suite imports the integration's modules, so a name missing from a
    top-level import fails collection loudly. A name imported inside a function
    does not: `backfill.py` and `bank.py` each carry one, and the branch that
    runs it may not run in a given test session -- it would surface as an
    `ImportError` in somebody's Home Assistant instead.

    This is deliberately about the library *in the tree*. The version in the
    manifest is the last released one until a release commit bumps it, so
    comparing against the published distribution would be red between every
    pair of releases and say nothing; the release itself is safe by
    construction, because the wheel and the manifest are built from the same
    tree.
    """
    missing: list[str] = []
    for path in sorted((ROOT / "custom_components" / "tariffkit").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if not module.startswith("tariffkit"):
                continue
            try:
                imported = importlib.import_module(module)
            except ImportError:
                missing.append(f"{path.name}:{node.lineno} {module}")
                continue
            for alias in node.names:
                if alias.name == "*" or hasattr(imported, alias.name):
                    continue
                try:
                    importlib.import_module(f"{module}.{alias.name}")
                except ImportError:
                    missing.append(f"{path.name}:{node.lineno} {module}.{alias.name}")

    assert not missing, "the integration imports names the library does not provide: " + "; ".join(
        missing
    )


def test_the_readme_names_every_extra_and_no_others() -> None:
    """An install line for an extra that does not exist, or a missing one.

    The README listed five extras when eight were declared, and the three it
    omitted -- `ha`, `influx`, `pge` -- are exactly the ones the meter and
    statement paths need, so following the README left `bill --source ha`
    uninstallable.
    """
    declared = set(
        tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "optional-dependencies"
        ]
    )
    shown = re.findall(r"tariffkit\[([a-z,]+)\]", (ROOT / "README.md").read_text(encoding="utf-8"))
    named = {extra for group in shown for extra in group.split(",")}

    assert not named - declared, f"README names extras that do not exist: {named - declared}"
    assert not declared - named, f"README omits declared extras: {declared - named}"


def _documented_invocations(text: str) -> list[tuple[int, list[str]]]:
    """Every documented `tariffkit ...` call, as (line, argv).

    Tokenised rather than string-searched. `\\` continuations are joined,
    because most real examples span lines and a scanner reading only the first
    reports a flagless `account source set` that is really three lines long --
    noise that hides genuine breakage. `ExecStart=/opt/.../bin/tariffkit` and a
    cron line count: a systemd unit in the docs was broken the same way a bare
    command was. Env-var prefixes (`TARIFFKIT_MQTT_PASSWORD=secret tariffkit
    ...`) are skipped over rather than parsed as arguments.
    """
    lines = text.splitlines()
    found: list[tuple[int, list[str]]] = []
    index = 0
    while index < len(lines):
        raw = lines[index].strip().removeprefix("$ ").strip()
        start_line = index + 1
        while raw.endswith("\\") and index + 1 < len(lines):
            index += 1
            raw = raw[:-1].rstrip() + " " + lines[index].strip()
        index += 1
        tokens = raw.split()
        # An installer names the distribution, not the command.
        if any(token in {"pip", "pipx", "uvx", "uv"} for token in tokens):
            continue
        at = next(
            (
                n
                for n, token in enumerate(tokens)
                if token == "tariffkit" or token.endswith("/tariffkit")
            ),
            None,
        )
        if at is None:
            continue
        # Everything before it must be an env assignment or a unit/cron prefix,
        # or this is prose mentioning a path rather than running anything.
        if any("=" not in token for token in tokens[:at]):
            continue
        found.append((start_line, tokens[at + 1 :]))
    return found


@pytest.mark.parametrize(
    "doc",
    ["README.md", "audit/README.md", *[f"docs/{p.name}" for p in sorted(DOCS.glob("*.md"))]],
)
def test_the_docs_only_show_commands_the_cli_has(doc: str) -> None:
    """Documented commands are parsed, because three of them did not exist.

    `tariffkit account init home` and `tariffkit account source home show ha`
    were in the released 0.8.1 README and had not been accepted since named
    profiles were removed; `tariffkit mqtt ... -v` put a top-level flag after
    the subcommand, in a code block and again in a systemd unit. Checked against
    the real parser rather than by running anything.
    """
    from tariffkit.cli import build_parser

    parser = build_parser()
    broken: list[str] = []
    for line_no, tokens in _documented_invocations((ROOT / doc).read_text(encoding="utf-8")):
        argv = list(itertools.takewhile(lambda token: not token.startswith("#"), tokens))
        # Placeholders a reader is meant to substitute.
        if not argv or any(
            a.startswith("<") or "$" in a or (a.isupper() and not a.startswith("-")) for a in argv
        ):
            continue
        try:
            parser.parse_args(argv)
        except SystemExit:
            broken.append(f"{doc}:{line_no}: {' '.join(argv)}")

    assert not broken, f"documented commands the CLI rejects: {broken}"
