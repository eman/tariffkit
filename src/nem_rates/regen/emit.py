"""Writing a vendored data file, and refusing to write a bad one.

Every dataset here ends the same way: render TOML, prove it is right, then
either write it or report that it differs. The proving is the point. A generator
writes key names as string literals and the library reads them back with a
second, independent set of literals -- two encodings of one schema that can
drift apart silently, which is a risk generation *introduces* and therefore has
to close. So each dataset supplies a check that loads what was rendered and
exercises it through the real consumer.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
#: Where every regenerator caches what it downloads. Outside the repo tree on
#: purpose: these are hundreds of megabytes of somebody else's documents, and a
#: cache inside the working tree survives only as long as nobody widens a
#: gitignore rule.
DEFAULT_CACHE = Path.home() / ".cache" / "nem-rates" / "regen"


def fmt(value: Any) -> str:
    """A TOML scalar. Floats keep five decimals, which is how rates are published."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.5f}".rstrip("0").rstrip(".") if value else "0.0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(fmt(v) for v in value) + "]"
    return f'"{value}"'


def table(name: str, values: dict[str, Any]) -> list[str]:
    return [f"[{name}]", *[f"{k} = {fmt(v)}" for k, v in values.items()]]


@dataclass(frozen=True, slots=True)
class Result:
    """What one dataset regeneration did."""

    label: str
    changed: bool
    failed: bool
    messages: tuple[str, ...] = ()

    def report(self) -> None:
        for message in self.messages:
            print(f"{self.label}: {message}")


def write_or_check(
    label: str,
    target: Path,
    body: str,
    *,
    check: bool,
    verify: Callable[[str], list[str]] | None = None,
    messages: list[str] | None = None,
) -> Result:
    """Verify ``body``, then write it or report that it differs.

    ``verify`` receives the rendered text and returns a list of problems; a
    non-empty list means nothing is written, in either mode. That is deliberate:
    a ``--check`` run that reported "changed" on output the library cannot read
    would send someone to regenerate a broken file.
    """
    notes = list(messages or [])
    if verify is not None:
        problems = verify(body)
        if problems:
            return Result(
                label,
                changed=False,
                failed=True,
                messages=(*notes, "REFUSING to write:", *[f"    {p}" for p in problems]),
            )

    if target.exists():
        try:
            unchanged = tomllib.loads(target.read_text(encoding="utf-8")) == tomllib.loads(body)
        except tomllib.TOMLDecodeError:
            unchanged = False
        if unchanged:
            return Result(
                label, changed=False, failed=False, messages=(*notes, f"unchanged ({target.name})")
            )

    where = target.relative_to(DATA_DIR)
    if check:
        return Result(label, changed=True, failed=False, messages=(*notes, f"CHANGED -> {where}"))

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return Result(label, changed=True, failed=False, messages=(*notes, f"wrote {where}"))
