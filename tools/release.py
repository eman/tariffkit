"""Prepare and validate TariffKit releases from the repository checkout."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parent.parent
PROJECT_FILE = Path("pyproject.toml")
LOCK_FILE = Path("uv.lock")
MANIFEST_FILE = Path("custom_components/tariffkit/manifest.json")
CHANGELOG_FILE = Path("CHANGELOG.md")
VERSIONED_DOCS = (
    Path("docs/containers.md"),
    Path("docs/home-assistant.md"),
    Path("docs/home-assistant-quality.md"),
)
CHANGE_TYPES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
_HEADING = re.compile(
    r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?(?: \[YANKED\])?$",
    re.MULTILINE,
)
_CHANGE_TYPE = re.compile(r"^### ([^\n]+)$", re.MULTILINE)
_RELEASE_LINK = re.compile(r"^\[([^\]]+)\]: .+$", re.MULTILINE)


class ReleaseError(Exception):
    """A release invariant was not satisfied."""


def parse_version(raw: str) -> Version:
    """Return a normalized release version within TariffKit's version policy."""
    try:
        version = Version(raw)
    except InvalidVersion as exc:
        raise ReleaseError(f"invalid release version {raw!r}") from exc
    if str(version) != raw:
        raise ReleaseError(f"version must use normalized PEP 440 spelling {str(version)!r}")
    if len(version.release) != 3 or version.epoch or version.dev or version.post or version.local:
        raise ReleaseError("version must be MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCHrcN")
    if version.pre is not None and version.pre[0] != "rc":
        raise ReleaseError("only rc prereleases are supported")
    return version


def project_version(root: Path = ROOT) -> Version:
    """Read the canonical project version."""
    with (root / PROJECT_FILE).open("rb") as handle:
        raw = tomllib.load(handle)["project"]["version"]
    if not isinstance(raw, str):
        raise ReleaseError("project.version must be a string")
    return parse_version(raw)


def _sections(changelog: str) -> list[tuple[str, str | None, str]]:
    matches = list(_HEADING.finditer(changelog))
    if not matches or matches[0].group(1) != "Unreleased":
        raise ReleaseError("CHANGELOG.md must begin with an Unreleased version section")
    sections: list[tuple[str, str | None, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else None
        sections.append((match.group(1), match.group(2), changelog[match.end() : end]))
    names = [section[0] for section in sections]
    if len(names) != len(set(names)):
        raise ReleaseError("CHANGELOG.md repeats a version section")
    for version, released_on, _body in sections[1:]:
        parse_version(version)
        if released_on is None:
            raise ReleaseError(f"changelog section {version} must have an ISO release date")
        try:
            date.fromisoformat(released_on)
        except ValueError as exc:
            raise ReleaseError(
                f"changelog section {version} has invalid release date {released_on!r}"
            ) from exc
    return sections


def _validate_change_types(body: str, *, section: str, allow_empty: bool) -> None:
    headings = _CHANGE_TYPE.findall(body)
    if not headings and allow_empty and not body.strip():
        return
    if not headings:
        raise ReleaseError(f"changelog section {section!r} has no change categories")
    unknown = [heading for heading in headings if heading not in CHANGE_TYPES]
    if unknown:
        raise ReleaseError(f"changelog section {section!r} has unknown categories: {unknown}")
    if len(headings) != len(set(headings)):
        raise ReleaseError(f"changelog section {section!r} repeats a change category")
    positions = [CHANGE_TYPES.index(heading) for heading in headings]
    if positions != sorted(positions):
        raise ReleaseError(f"changelog section {section!r} categories must follow {CHANGE_TYPES}")


def _normalize_change_types(body: str) -> str:
    matches = list(_CHANGE_TYPE.finditer(body))
    if not matches:
        raise ReleaseError("Unreleased has no change categories")
    if body[: matches[0].start()].strip():
        raise ReleaseError("Unreleased text must belong to a named change category")
    grouped: dict[str, list[str]] = {heading: [] for heading in CHANGE_TYPES}
    for index, match in enumerate(matches):
        heading = match.group(1)
        if heading == "Notes":
            heading = "Changed"
        if heading not in grouped:
            raise ReleaseError(f"Unreleased has unknown category {heading!r}")
        end = matches[index + 1].start() if index + 1 < len(matches) else None
        content = body[match.end() : end]
        if content.strip():
            grouped[heading].append(content.strip())
    return "\n\n".join(
        f"### {heading}\n{'\n'.join(parts)}" for heading, parts in grouped.items() if parts
    )


def check(
    root: Path = ROOT,
    *,
    expected_version: str | None = None,
    tag: str | None = None,
) -> Version:
    """Validate version, changelog, and Home Assistant release identity."""
    version = project_version(root)
    if expected_version is not None and version != parse_version(expected_version):
        raise ReleaseError(f"project version is {version}, expected {expected_version}")
    if tag is not None and tag != f"v{version}":
        raise ReleaseError(f"release tag must be v{version}, got {tag!r}")

    manifest = json.loads((root / MANIFEST_FILE).read_text(encoding="utf-8"))
    if manifest.get("version") != str(version):
        raise ReleaseError("Home Assistant manifest version does not match project.version")
    if manifest.get("requirements") != [f"tariffkit=={version}"]:
        raise ReleaseError("Home Assistant manifest must require the exact project version")

    changelog = (root / CHANGELOG_FILE).read_text(encoding="utf-8")
    sections = _sections(changelog)
    _validate_change_types(sections[0][2], section="Unreleased", allow_empty=True)
    if len(sections) < 2 or sections[1][0] != str(version):
        raise ReleaseError(f"the newest changelog release must be {version}")
    _validate_change_types(sections[1][2], section=str(version), allow_empty=False)
    return version


def _update_manifest(root: Path, old: Version, new: Version) -> None:
    path = root / MANIFEST_FILE
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("version") != str(old):
        raise ReleaseError("Home Assistant manifest does not match the version being replaced")
    if manifest.get("requirements") != [f"tariffkit=={old}"]:
        raise ReleaseError("Home Assistant requirement does not match the version being replaced")
    manifest["version"] = str(new)
    manifest["requirements"] = [f"tariffkit=={new}"]
    path.write_text(f"{json.dumps(manifest, indent=2)}\n", encoding="utf-8")


def _update_docs(root: Path, old: Version, new: Version) -> None:
    old_text = str(old)
    for relative in VERSIONED_DOCS:
        path = root / relative
        content = path.read_text(encoding="utf-8")
        if old_text not in content:
            continue
        path.write_text(content.replace(old_text, str(new)), encoding="utf-8")


def _cut_changelog(root: Path, version: Version, released_on: date, body: str) -> None:
    path = root / CHANGELOG_FILE
    changelog = path.read_text(encoding="utf-8")
    sections = _sections(changelog)
    if any(section[0] == str(version) for section in sections):
        raise ReleaseError(f"CHANGELOG.md already contains {version}")
    first_end = _HEADING.search(changelog)
    if first_end is None:
        raise AssertionError("validated changelog lost its Unreleased heading")
    remainder_start = first_end.end() + len(sections[0][2])
    replacement = f"## [Unreleased]\n\n## [{version}] - {released_on.isoformat()}\n\n{body}\n\n"
    updated = f"{changelog[: first_end.start()]}{replacement}{changelog[remainder_start:]}"

    links = {match.group(1): match.group(0) for match in _RELEASE_LINK.finditer(updated)}
    updated = _RELEASE_LINK.sub("", updated).rstrip()
    links["Unreleased"] = (
        f"[Unreleased]: https://github.com/eman/tariffkit/compare/v{version}...HEAD"
    )
    links[str(version)] = f"[{version}]: https://github.com/eman/tariffkit/releases/tag/v{version}"
    ordered_links = [links["Unreleased"], links[str(version)]]
    ordered_links.extend(
        value for key, value in links.items() if key not in {"Unreleased", str(version)}
    )
    rendered_links = "\n".join(ordered_links)
    path.write_text(f"{updated}\n\n{rendered_links}\n", encoding="utf-8")


def prepare(
    raw_version: str,
    *,
    root: Path = ROOT,
    released_on: date | None = None,
    update_project: Callable[[Version], None],
) -> Version:
    """Prepare reviewed source files for a new release."""
    target = parse_version(raw_version)
    current = project_version(root)
    if target <= current:
        raise ReleaseError(f"new version {target} must be greater than current version {current}")
    changelog = (root / CHANGELOG_FILE).read_text(encoding="utf-8")
    normalized_changes = _normalize_change_types(_sections(changelog)[0][2].strip())
    _validate_change_types(normalized_changes, section="Unreleased", allow_empty=False)
    update_project(target)
    _update_manifest(root, current, target)
    _update_docs(root, current, target)
    _cut_changelog(
        root,
        target,
        released_on or datetime.now(tz=UTC).date(),
        normalized_changes,
    )
    check(root, expected_version=str(target))
    return target


def notes(raw_version: str, root: Path = ROOT) -> str:
    """Return the curated release notes for one version."""
    version = parse_version(raw_version)
    changelog = (root / CHANGELOG_FILE).read_text(encoding="utf-8")
    matches = [section for section in _sections(changelog) if section[0] == str(version)]
    if len(matches) != 1 or matches[0][1] is None:
        raise ReleaseError(f"CHANGELOG.md has no dated {version} release")
    body = matches[0][2].strip()
    _validate_change_types(body, section=str(version), allow_empty=False)
    return f"## TariffKit {version}\n\n{body}\n"


def ensure_available(raw_version: str, repository: str) -> None:
    """Fail if a package index already contains the proposed version."""
    version = parse_version(raw_version)
    hosts = {
        "pypi": "https://pypi.org",
        "testpypi": "https://test.pypi.org",
    }
    try:
        host = hosts[repository]
    except KeyError as exc:
        raise ReleaseError(f"unknown package repository {repository!r}") from exc
    url = f"{host}/pypi/tariffkit/{version}/json"
    try:
        with urlopen(url, timeout=15) as response:
            status = response.status
    except HTTPError as exc:
        if exc.code == 404:
            return
        raise ReleaseError(f"{repository} availability check returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise ReleaseError(f"could not query {repository}: {exc.reason}") from exc
    if status == 200:
        raise ReleaseError(f"tariffkit {version} already exists on {repository}")
    raise ReleaseError(f"{repository} availability check returned unexpected HTTP {status}")


def _uv_version(root: Path) -> Callable[[Version], None]:
    def update(version: Version) -> None:
        subprocess.run(
            ["uv", "version", str(version), "--no-sync"],
            cwd=root,
            check=True,
        )

    return update


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.release")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="cut a new release in source files")
    prepare_parser.add_argument("version")
    prepare_parser.add_argument("--date", type=date.fromisoformat, dest="released_on")

    check_parser = subparsers.add_parser("check", help="validate release identity")
    check_parser.add_argument("--version")
    check_parser.add_argument("--tag")

    notes_parser = subparsers.add_parser("notes", help="print one changelog release")
    notes_parser.add_argument("version")

    available_parser = subparsers.add_parser(
        "available", help="check that an index has not published a version"
    )
    available_parser.add_argument("version")
    available_parser.add_argument("--repository", choices=("pypi", "testpypi"), required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            prepared = prepare(
                args.version,
                released_on=args.released_on,
                update_project=_uv_version(ROOT),
            )
            print(f"prepared TariffKit {prepared}")
        elif args.command == "check":
            checked = check(expected_version=args.version, tag=args.tag)
            print(f"release identity is consistent for TariffKit {checked}")
        elif args.command == "notes":
            sys.stdout.write(notes(args.version))
        elif args.command == "available":
            ensure_available(args.version, args.repository)
            print(f"tariffkit {args.version} is available on {args.repository}")
        else:
            raise AssertionError(f"unhandled release command {args.command!r}")
    except (OSError, ReleaseError, subprocess.CalledProcessError) as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
