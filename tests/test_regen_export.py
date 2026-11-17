"""Collapsing PG&E's export-rate CSVs, on a small file built to their shape.

The real files are 40 MB each and not vendored, so these build two years of
rows in the upstream layout and drive :func:`normalize_vintage` and
:func:`retain_dropped_years` directly. What they pin is the two upstream
changes that arrived in November 2026: the repeated fall-back hour relabelled
from HS2 to HS1, and a floating file that no longer covers the current year.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tariffkit.timeutil import PACIFIC, is_repeated_hour
from tools.regen.export import (
    EXPECTED_HEADER,
    MONTHS,
    NormalizationError,
    normalize_vintage,
    retain_dropped_years,
)

RINS = {"delivery": "USCA-PGXX-NB0-0000", "generation": "USCA-XXPG-NB0-0000"}


def _value(month: str, day_type: str, hour: int, component: str) -> str:
    # Distinct per cell and per component, so a misplaced row is a conflict.
    base = MONTHS.index(month) * 100 + (50 if day_type == "Weekend" else 0) + hour
    return f"{base / 10_000 + (0.5 if component == 'generation' else 0):.5f}"


def build_csv(years: tuple[int, int], fall_back_label: dict[int, int]) -> bytes:
    """Hourly rows covering ``years`` in Pacific time, laid out as upstream does."""
    start = datetime(years[0], 1, 1, tzinfo=PACIFIC).astimezone(UTC)
    end = datetime(years[1] + 1, 1, 1, tzinfo=PACIFIC).astimezone(UTC)
    lines = [",".join(EXPECTED_HEADER)]
    moment = start
    while moment < end:
        pacific = moment.astimezone(PACIFIC)
        hour = fall_back_label[pacific.year] if is_repeated_hour(pacific) else pacific.hour
        day_type = "Weekend" if pacific.weekday() >= 5 else "Weekday"
        month = MONTHS[pacific.month - 1]
        stamp_date = f"{moment.month}/{moment.day}/{moment.year}"
        stamp_time = f"{moment.hour}:00:00"
        for component, rin in RINS.items():
            lines.append(
                ",".join(
                    [
                        rin,
                        "NBT00",
                        stamp_date,
                        stamp_time,
                        stamp_date,
                        f"{moment.hour}:59:59",
                        str(pacific.isoweekday()),
                        str(pacific.isoweekday()),
                        f"{month} {day_type} HS{hour}",
                        _value(month, day_type, hour, component),
                        "Export $/kWh",
                        "TOU",
                        "ALL",
                    ]
                )
            )
        moment += timedelta(hours=1)
    return ("\n".join(lines) + "\n").encode()


def payload_for(fall_back_label: dict[int, int], years: tuple[int, int]) -> dict[str, Any]:
    result = normalize_vintage(build_csv(years, fall_back_label), "synthetic.csv")
    payload = result["payload"]
    assert isinstance(payload, dict)
    return payload


class TestFallBackLabel:
    @pytest.mark.parametrize("label", [1, 2])
    def test_either_label_round_trips(self, label: int) -> None:
        payload = payload_for({2027: label, 2028: label}, (2027, 2028))
        assert payload["fall_back_hour"] == {"2027": label, "2028": label}
        assert payload["exact_through"] == 2028

    def test_the_label_is_read_per_year(self) -> None:
        """The 2025 and 2026 vintages switch from HS2 to HS1 part-way through."""
        payload = payload_for({2027: 2, 2028: 1}, (2027, 2028))
        assert payload["fall_back_hour"] == {"2027": 2, "2028": 1}
        assert payload["exact_through"] == 2028

    def test_a_label_neither_convention_explains_is_not_exact(self) -> None:
        payload = payload_for({2027: 2, 2028: 3}, (2027, 2028))
        assert payload["exact_through"] == 2027
        assert "2028" not in payload["fall_back_hour"]

    def test_a_file_with_no_exact_year_is_refused(self) -> None:
        """Writing it would report every price this vintage returns as unverified."""
        with pytest.raises(NormalizationError, match="no year reproduces"):
            payload_for({2027: 3, 2028: 2}, (2027, 2028))


SOURCE_OLD = {"member_sha256": "old", "archive_sha256": "a1", "member": "m", "url": "u"}
SOURCE_OLDER = {"member_sha256": "older", "archive_sha256": "a0", "member": "m", "url": "u"}


def _vendored(years: list[int], exact_through: int, **extra: Any) -> dict[str, Any]:
    return {
        "years": years,
        "exact_through": exact_through,
        "data": {str(y): {"delivery": f"old-{y}"} for y in years},
        "holidays": {str(y): [f"{y}-01-01"] for y in years},
        "source": SOURCE_OLD,
        **extra,
    }


def _upstream(years: list[int]) -> dict[str, Any]:
    return {
        "years": years,
        "exact_through": years[-1],
        "data": {str(y): {"delivery": f"new-{y}"} for y in years},
        "holidays": {str(y): [f"{y}-01-01"] for y in years},
        "fall_back_hour": {str(y): 1 for y in years},
        "source": {"member_sha256": "new"},
    }


class TestRetainDroppedYears:
    def test_keeps_the_leading_years_upstream_dropped(self) -> None:
        payload = _upstream([2027, 2028])
        retained = retain_dropped_years(payload, _vendored([2025, 2026, 2027], 2027), "m")

        assert retained == [2025, 2026]
        assert payload["years"] == [2025, 2026, 2027, 2028]
        assert payload["data"]["2026"] == {"delivery": "old-2026"}
        # A year upstream still publishes is upstream's, never the old copy's.
        assert payload["data"]["2027"] == {"delivery": "new-2027"}
        assert payload["holidays"]["2025"] == ["2025-01-01"]
        # Vendored before the label was recorded, so verified under HS2.
        assert payload["fall_back_hour"]["2025"] == 2
        assert payload["retained"] == [{"source": SOURCE_OLD, "years": [2025, 2026]}]

    def test_nothing_dropped_changes_nothing(self) -> None:
        payload = _upstream([2026, 2027])
        before = copy.deepcopy(payload)
        assert retain_dropped_years(payload, _vendored([2026, 2027], 2027), "m") == []
        assert retain_dropped_years(payload, None, "m") == []
        assert payload == before

    def test_rerunning_keeps_the_original_provenance(self) -> None:
        """A year carried forward twice still names the file it was read from."""
        first = _upstream([2027, 2028])
        retain_dropped_years(first, _vendored([2025, 2026, 2027], 2027), "m")
        source_mid = {"member_sha256": "mid", "archive_sha256": "a2", "member": "m", "url": "u"}
        first["source"] = source_mid

        second = _upstream([2028, 2029])
        retained = retain_dropped_years(second, first, "m")
        assert retained == [2025, 2026, 2027]
        assert second["retained"] == [
            {"source": SOURCE_OLD, "years": [2025, 2026]},
            {"source": source_mid, "years": [2027]},
        ]
        assert second["fall_back_hour"] == {"2025": 2, "2026": 2, "2027": 1, "2028": 1, "2029": 1}

    def test_provenance_is_grouped_by_source(self) -> None:
        vendored = _vendored(
            [2024, 2025, 2026],
            2026,
            retained=[{"source": SOURCE_OLDER, "years": [2024]}],
            fall_back_hour={"2024": 2, "2025": 2, "2026": 1},
        )
        payload = _upstream([2027])
        retain_dropped_years(payload, vendored, "m")
        assert payload["retained"] == [
            {"source": SOURCE_OLDER, "years": [2024]},
            {"source": SOURCE_OLD, "years": [2025, 2026]},
        ]
        assert payload["fall_back_hour"]["2026"] == 1

    def test_a_gap_is_refused(self) -> None:
        with pytest.raises(NormalizationError, match="gap"):
            retain_dropped_years(_upstream([2028]), _vendored([2025, 2026], 2026), "m")

    def test_an_unverified_year_is_not_carried_forward(self) -> None:
        with pytest.raises(NormalizationError, match="not verified exact"):
            retain_dropped_years(_upstream([2027]), _vendored([2025, 2026], 2025), "m")
