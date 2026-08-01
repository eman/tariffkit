"""Access to the vendored rate data shipped inside the package."""

from __future__ import annotations

import gzip
import json
from importlib.resources import files
from typing import Any

from ..errors import DataError


def _resource(relative: str) -> Any:
    resource = files(__package__)
    for part in relative.split("/"):
        resource = resource / part
    return resource


def read_data_text(relative: str) -> str:
    """Read a text data file, e.g. ``holidays.toml``."""
    try:
        text: str = _resource(relative).read_text(encoding="utf-8")
        return text
    except FileNotFoundError as exc:
        raise DataError(f"vendored data file missing: {relative}") from exc


def read_data_json_gz(relative: str) -> dict[str, Any]:
    """Read a gzipped JSON data file, e.g. ``export/pge/nbt26.json.gz``."""
    try:
        raw = _resource(relative).read_bytes()
    except FileNotFoundError as exc:
        raise DataError(f"vendored data file missing: {relative}") from exc
    payload: dict[str, Any] = json.loads(gzip.decompress(raw))
    return payload


def data_exists(relative: str) -> bool:
    return bool(_resource(relative).is_file())
