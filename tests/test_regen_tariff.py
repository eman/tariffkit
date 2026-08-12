"""Parsing PG&E tariff sheets.

The extraction functions take text, not PDFs, so everything here runs on
fragments copied verbatim from the three sheets rather than on a vendored
binary. What it pins is the ways the three schedules differ from one another --
column counts, season-row layouts, wrapped labels -- since those are what a
parser written against one sheet gets wrong on the next.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import regen_tariff as rt

# Verbatim from Schedule E-ELEC Sheet 3.
EELEC_UNBUNDLED = """UNBUNDLING OF TOTAL RATES |
Energy Rates by Component ($ per kWh) PEAK PART-PEAK OFF-PEAK |
Generation: |
Summer Usage $0.26299  $0.16388  $0.11878  |
Winter Usage $0.10086  $0.08089  $0.06754  |
Distribution**: |
Summer Usage $0.23199 (I) $0.16922 (I) $0.15764 (I) |
Winter Usage $0.16261 (I) $0.16049 (I) $0.15998 (I) |
Transmission* (all usage) $0.04638  $0.04638  $0.04638  |
Nuclear Decommissioning (all usage) ($0.00002)  ($0.00002)  ($0.00002)  |
Bundled Power Charge Indifference
Adjustment (all usage)***
($0.01011)  ($0.01011)  ($0.01011)  (L)
"""

# Verbatim from Schedule E-TOU-C Sheet 3: two columns, different season labels.
ETOUC_UNBUNDLED = """UNBUNDLING OF E-TOU-C TOTAL RATES
Energy Rates by Component ($ per kWh) PEAK OFF-PEAK
Generation:
Summer (all usage) $0.20782   $0.10482
Winter (all usage) $0.13710   $0.11042
Distribution**:
Summer (all usage) $0.20388  (R) $0.18388  (R)
Winter (all usage) $0.14977  (R) $0.14645  (R)
Conservation Incentive Adjustment (Baseline Usage) ($0.02786) (I)
Conservation Incentive Adjustment (Over Baseline Usage) $0.05354 (R)
Transmission* (all usage) $0.04638
"""

# E-ELEC/EV2-A put the season on the rate line...
EELEC_TOTALS = """TOTAL BUNDLED RATES
Total Energy Rates ($ per kWh) PEAK PART-PEAK OFF-PEAK
Summer Usage $0.55214 (R) $0.39026 (R) $0.33358 (R)
Winter Usage $0.32063 (R) $0.29854 (R) $0.28468 (R)
Base Services Charge Rates ($ per customer per day)
Income Tier 1 $0.19713 (N)
Income Tier 2 $0.39688 (N)
Income Tier 3 $0.79343 (N)
"""

# ...E-TOU-C puts it on a line of its own, above a "Total Usage" row.
ETOUC_TOTALS = """Total Energy Rates ($ per kWh) PEAK OFF-PEAK
Summer
Total Usage $0.52240 (R) $0.39940 (R)
Baseline Credit (Applied to Baseline Usage Only) ($0.08140) (I) ($0.08140) (I)
Winter
Total Usage $0.39757 (R) $0.36757 (R)
Baseline Credit (Applied to Baseline Usage Only) ($0.08140) (I) ($0.08140) (I)
Base Services Charge Rates ($ per customer per day)
Income Tier 1 $0.19713 (N)
Income Tier 2 $0.39688 (N)
Income Tier 3 $0.79343 (N)
"""

BASELINE_QUANTITIES = """BASELINE QUANTITIES (kWh PER DAY) |
Territory* Tier 1 Tier 1 Tier 1 Tier 1 |
P 13.5 (R) 11.0 (R) 15.2 (R) 26.0 (R) |
Q 9.8 (R) 11.0 (R) 8.5 (R) 26.0 (R) |
Z 5.9 (R) 7.8 (R) 6.7 (R) 15.7 (R) |
"""


def sheet(text: str, number: int = 3, advice: str = "7846-E", eff: date | None = None) -> rt.Sheet:
    return rt.Sheet(
        number=number, advice_letter=advice, effective=eff or date(2026, 3, 1), text=text
    )


class TestCells:
    def test_a_parenthesised_value_is_negative(self) -> None:
        assert rt.cells("($0.00002)") == [-0.00002]

    def test_change_markers_are_not_values(self) -> None:
        # (I)/(R)/(N)/(L) mark increased/reduced/new/left-unchanged, not money.
        assert rt.cells("$0.23199 (I) $0.16922 (R)") == [0.23199, 0.16922]

    def test_a_row_of_three(self) -> None:
        assert rt.cells("$0.04638  $0.04638  $0.04638") == [0.04638] * 3


class TestLabels:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Transmission* (all usage)", "transmission"),
            ("New System Generation Charge (all usage)**", "new system generation charge"),
            ("Recovery Bond Credit (all usage)", "recovery bond credit"),
        ],
    )
    def test_footnotes_and_qualifiers_are_stripped(self, raw: str, expected: str) -> None:
        assert rt.clean_label(raw) == expected

    def test_a_wrapped_label_is_joined_to_its_values(self) -> None:
        # The EV2-A and E-ELEC sheets wrap this one across three lines.
        lines = dict(rt._rate_lines(EELEC_UNBUNDLED))
        assert any("Indifference" in label for label in lines)
        assert any(v == [-0.01011] * 3 for v in lines.values())


class TestUnbundled:
    def test_three_column_schedule(self) -> None:
        periods, energy, adders = rt.extract_unbundled(sheet(EELEC_UNBUNDLED))
        assert periods == ["peak", "part_peak", "off_peak"]
        assert energy["summer"]["peak"] == {"generation": 0.26299, "distribution": 0.23199}
        assert energy["winter"]["off_peak"]["generation"] == 0.06754
        assert adders["transmission"] == 0.04638
        assert adders["nuclear_decommissioning"] == -0.00002
        assert adders["bundled_pcia"] == -0.01011

    def test_two_column_schedule_has_no_part_peak(self) -> None:
        # E-TOU-C genuinely has no part-peak; inventing one would misprice it.
        periods, energy, _ = rt.extract_unbundled(sheet(ETOUC_UNBUNDLED))
        assert periods == ["peak", "off_peak"]
        assert set(energy["summer"]) == {"peak", "off_peak"}
        assert energy["summer"]["peak"]["distribution"] == 0.20388

    def test_generation_and_distribution_are_not_confused(self) -> None:
        # Both sections use the same "Summer Usage" label, so only the section
        # header above them tells the two apart.
        _, energy, _ = rt.extract_unbundled(sheet(EELEC_UNBUNDLED))
        assert energy["summer"]["peak"]["generation"] != energy["summer"]["peak"]["distribution"]

    def test_a_column_count_mismatch_is_an_error(self) -> None:
        broken = EELEC_UNBUNDLED.replace("$0.26299  $0.16388  $0.11878", "$0.26299  $0.16388")
        with pytest.raises(rt.ExtractionError, match="values for"):
            rt.extract_unbundled(sheet(broken))


class TestTotals:
    def test_season_on_the_rate_line(self) -> None:
        got = rt.extract_totals([sheet(EELEC_TOTALS, number=2)], ["peak", "part_peak", "off_peak"])
        assert got["summer"]["peak"] == 0.55214
        assert got["winter"]["off_peak"] == 0.28468

    def test_season_on_its_own_line_above_a_total_usage_row(self) -> None:
        got = rt.extract_totals([sheet(ETOUC_TOTALS, number=2)], ["peak", "off_peak"])
        assert got["summer"] == {"peak": 0.52240, "off_peak": 0.39940}
        assert got["winter"] == {"peak": 0.39757, "off_peak": 0.36757}

    def test_the_baseline_credit_row_is_not_mistaken_for_a_total(self) -> None:
        got = rt.extract_totals([sheet(ETOUC_TOTALS, number=2)], ["peak", "off_peak"])
        assert all(v > 0 for by_period in got.values() for v in by_period.values())

    def test_a_sheet_without_both_seasons_is_an_error(self) -> None:
        half = ETOUC_TOTALS.split("Winter")[0]
        with pytest.raises(rt.ExtractionError, match="expected summer and winter"):
            rt.extract_totals([sheet(half, number=2)], ["peak", "off_peak"])


class TestBaseServicesCharge:
    def test_three_income_tiers(self) -> None:
        got = rt.extract_base_services_charge([sheet(EELEC_TOTALS, number=2)])
        assert got == {"tier_1": 0.19713, "tier_2": 0.39688, "tier_3": 0.79343}


class TestBaseline:
    def test_the_credit_is_the_spread_between_the_two_cia_rates(self) -> None:
        # The sheet never prints the credit as one number in this table; the
        # bill does. 0.05354 - (-0.02786) = 0.08140, which the totals page then
        # confirms as "Baseline Credit ($0.08140)".
        adders: dict[str, float] = {}
        got = rt.extract_baseline([sheet(ETOUC_UNBUNDLED)], adders)
        assert got["within_rate"] == -0.02786
        assert got["over_rate"] == 0.05354
        assert got["credit"] == pytest.approx(0.08140)
        assert adders["conservation_incentive_adjustment"] == 0.05354

    def test_quantities_split_basic_from_all_electric(self) -> None:
        adders: dict[str, float] = {}
        got = rt.extract_baseline([sheet(ETOUC_UNBUNDLED + BASELINE_QUANTITIES)], adders)
        assert got["quantities"]["basic"]["P"] == {"summer": 13.5, "winter": 11.0}
        assert got["quantities"]["all_electric"]["P"] == {"summer": 15.2, "winter": 26.0}
        assert got["quantities"]["all_electric"]["Z"]["winter"] == 15.7

    def test_a_schedule_without_a_baseline_yields_nothing(self) -> None:
        assert rt.extract_baseline([sheet(EELEC_UNBUNDLED)], {}) == {}


class TestVerify:
    def build(self, adders: dict[str, float]) -> rt.Extracted:
        return rt.Extracted(
            periods=["peak"],
            energy={"summer": {"peak": {"generation": 0.5, "distribution": 0.4}}},
            adders=adders,
            totals={"summer": {"peak": 1.0}},
        )

    def test_reconciling_components_pass(self) -> None:
        assert rt.verify(self.build({"transmission": 0.1})) == []

    def test_a_shortfall_is_reported_with_both_figures(self) -> None:
        problems = rt.verify(self.build({"transmission": 0.05}))
        assert len(problems) == 1
        assert "0.95000" in problems[0] and "1.00000" in problems[0]

    def test_the_conservation_incentive_adjustment_counts_toward_the_total(self) -> None:
        # E-TOU-C's published total is the over-baseline price, so excluding the
        # CIA left every cell short by exactly its value.
        assert rt.verify(self.build({"conservation_incentive_adjustment": 0.1})) == []

    def test_a_missing_total_is_reported_rather_than_skipped(self) -> None:
        data = self.build({"transmission": 0.1})
        data.totals = {}
        assert "no published total" in rt.verify(data)[0]


class TestPickEffective:
    def test_the_sheet_carrying_the_rates_wins_over_a_later_reissue(self) -> None:
        # All three schedules reissued their totals page as 7921-E effective
        # 2026-06-01 while the unbundled table stayed 7846-E effective
        # 2026-03-01. Dating the snapshot June would leave April unpriceable.
        data = rt.Extracted(periods=["peak"])
        data.rates_effective = date(2026, 3, 1)
        data.provenance = [(2, "7921-E", date(2026, 6, 1)), (3, "7846-E", date(2026, 3, 1))]
        assert rt.pick_effective(data) == date(2026, 3, 1)

    def test_an_undated_sheet_is_an_error_rather_than_today(self) -> None:
        with pytest.raises(rt.ExtractionError, match="no effective date"):
            rt.pick_effective(rt.Extracted(periods=["peak"]))


def test_every_vendored_snapshot_still_reconciles() -> None:
    """The invariant the generator enforces must hold for what is committed."""
    import tomllib

    root = Path(__file__).resolve().parent.parent / "src" / "nem_rates" / "data" / "tariff" / "pge"
    files = sorted(root.rglob("*.toml"))
    assert files, "no vendored tariff snapshots found"
    for path in files:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        flat = sum(data["adders"].values())
        for season, by_period in data["energy"].items():
            for period, components in by_period.items():
                got = sum(components.values()) + flat
                want = data["totals"][season][period]
                assert got == pytest.approx(want, abs=5e-6), f"{path.name} {season}.{period}"
