"""Release preparation and identity checks."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from urllib.error import HTTPError

import pytest

from tools.release import (
    ReleaseError,
    check,
    ensure_available,
    notes,
    parse_version,
    prepare,
)


def repository(tmp_path: Path, *, changelog: str | None = None) -> Path:
    (tmp_path / "custom_components/tariffkit").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "tariffkit"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        'name = "tariffkit"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "custom_components/tariffkit/manifest.json").write_text(
        json.dumps(
            {
                "domain": "tariffkit",
                "version": "0.1.0",
                "requirements": ["tariffkit==0.1.0"],
            }
        ),
        encoding="utf-8",
    )
    for name in ("containers.md", "home-assistant.md", "home-assistant-quality.md"):
        (tmp_path / "docs" / name).write_text("tariffkit==0.1.0\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        changelog
        or """# Changelog

## [Unreleased]

### Added
- A useful feature.

### Fixed
- A bad defect.

## [0.1.0] - 2026-07-28

### Added
- Initial release.
""",
        encoding="utf-8",
    )
    return tmp_path


def update_project(root: Path, version: str) -> None:
    project = root / "pyproject.toml"
    project.write_text(
        re.sub(
            r'(?m)^version = "[^"]+"$',
            f'version = "{version}"',
            project.read_text(encoding="utf-8"),
            count=1,
        ),
        encoding="utf-8",
    )
    lock = root / "uv.lock"
    lock.write_text(
        re.sub(
            r'(?m)^version = "[^"]+"$',
            f'version = "{version}"',
            lock.read_text(encoding="utf-8"),
            count=1,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("version", ["0.2.0", "1.0.0", "1.2.3rc1"])
def test_parse_version_accepts_release_policy(version: str) -> None:
    assert str(parse_version(version)) == version


@pytest.mark.parametrize("version", ["0.2", "v0.2.0", "1.2.3-rc.1", "1.2.3.post1", "1.2.3a1"])
def test_parse_version_rejects_unsupported_spelling(version: str) -> None:
    with pytest.raises(ReleaseError):
        parse_version(version)


def test_prepare_synchronizes_identity_and_cuts_changelog(tmp_path: Path) -> None:
    root = repository(tmp_path)

    prepared = prepare(
        "0.2.0",
        root=root,
        released_on=date(2026, 8, 15),
        update_project=lambda version: update_project(root, str(version)),
    )

    assert str(prepared) == "0.2.0"
    assert check(root, expected_version="0.2.0", tag="v0.2.0") == prepared
    manifest = json.loads(
        (root / "custom_components/tariffkit/manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["requirements"] == ["tariffkit==0.2.0"]
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]\n\n## [0.2.0] - 2026-08-15" in changelog
    assert "[Unreleased]: https://github.com/eman/tariffkit/compare/v0.2.0...HEAD" in changelog
    assert "### Added\n- A useful feature." in notes("0.2.0", root)


def test_check_rejects_manifest_drift(tmp_path: Path) -> None:
    root = repository(tmp_path)
    manifest = root / "custom_components/tariffkit/manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["requirements"] = ["tariffkit>=0.1.0"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReleaseError, match="exact project version"):
        check(root)


@pytest.mark.parametrize(
    ("heading", "message"),
    [
        ("## [0.1.0] - 2026-07-28", "repeats a version"),
        ("## [0.0.9] - 2026-99-99", "invalid release date"),
    ],
)
def test_check_rejects_malformed_release_history(
    tmp_path: Path, heading: str, message: str
) -> None:
    root = repository(tmp_path)
    changelog = root / "CHANGELOG.md"
    changelog.write_text(
        f"{changelog.read_text(encoding='utf-8')}\n{heading}\n\n### Fixed\n- Defect.\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseError, match=message):
        check(root)


def test_check_accepts_yanked_release_marker(tmp_path: Path) -> None:
    root = repository(tmp_path)
    changelog = root / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text(encoding="utf-8").replace(
            "## [0.1.0] - 2026-07-28",
            "## [0.1.0] - 2026-07-28 [YANKED]",
        ),
        encoding="utf-8",
    )

    check(root)


def test_index_availability_accepts_only_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def not_found(url: str, *, timeout: int) -> None:
        raise HTTPError(url, 404, "not found", {}, None)

    monkeypatch.setattr("tools.release.urlopen", not_found)
    ensure_available("0.2.0", "pypi")


def test_index_availability_rejects_existing_version(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr("tools.release.urlopen", lambda url, timeout: Response())
    with pytest.raises(ReleaseError, match="already exists"):
        ensure_available("0.2.0", "testpypi")


@pytest.mark.parametrize(
    "body",
    [
        ("### Added\n- One.\n\n### Added\n- Two.", "repeats"),
        ("### Fixed\n- One.\n\n### Added\n- Two.", "must follow"),
    ],
)
def test_prepare_normalizes_repeated_or_unordered_categories(
    tmp_path: Path, body: tuple[str, str]
) -> None:
    root = repository(
        tmp_path,
        changelog=f"""# Changelog

## [Unreleased]

{body[0]}

## [0.1.0] - 2026-07-28

### Added
- Initial release.
""",
    )

    prepare(
        "0.2.0",
        root=root,
        update_project=lambda version: update_project(root, str(version)),
    )
    check(root)


def test_prepare_rejects_unknown_unreleased_category(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        changelog="""# Changelog

## [Unreleased]

### Internal
- One.

## [0.1.0] - 2026-07-28

### Added
- Initial release.
""",
    )

    with pytest.raises(ReleaseError, match="unknown"):
        prepare(
            "0.2.0",
            root=root,
            update_project=lambda version: update_project(root, str(version)),
        )
