"""Rebuilding the vendored rate data from published documents.

The extraction functions take text, not PDFs, so everything here runs on
fragments copied verbatim from the three sheets rather than on a vendored
binary. What it pins is the ways the three schedules differ from one another --
column counts, season-row layouts, wrapped labels -- since those are what a
parser written against one sheet gets wrong on the next.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("pypdf")

from nem_rates.regen import accplus, cca, franchise, nsc, providers, sheets, tax
from nem_rates.regen import tariff as rt
from nem_rates.regen.sheets import ExtractionError

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
    return sheets.Page(
        index=0,
        sheet_number=number,
        advice_letter=advice,
        effective=eff or date(2026, 3, 1),
        text=text,
    )


class TestCells:
    def test_a_parenthesised_value_is_negative(self) -> None:
        assert sheets.cells("($0.00002)") == [-0.00002]

    def test_change_markers_are_not_values(self) -> None:
        # (I)/(R)/(N)/(L) mark increased/reduced/new/left-unchanged, not money.
        assert sheets.cells("$0.23199 (I) $0.16922 (R)") == [0.23199, 0.16922]

    def test_a_row_of_three(self) -> None:
        assert sheets.cells("$0.04638  $0.04638  $0.04638") == [0.04638] * 3


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
        assert sheets.clean_label(raw) == expected

    def test_a_wrapped_label_is_joined_to_its_values(self) -> None:
        # The EV2-A and E-ELEC sheets wrap this one across three lines.
        lines = dict(sheets.rate_lines(EELEC_UNBUNDLED))
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
        with pytest.raises(ExtractionError, match="values for"):
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
        with pytest.raises(ExtractionError, match="expected summer and winter"):
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
        with pytest.raises(ExtractionError, match="no effective date"):
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
                # One unit in the last published decimal: the sheet rounds its
                # total independently of the components, so a vintage can miss
                # by an ulp. See regen.tariff.ROUNDING_TOLERANCE.
                assert got == pytest.approx(want, abs=rt.ROUNDING_TOLERANCE), (
                    f"{path.relative_to(root)} {season}.{period}"
                )


# Verbatim from MCE's residential rate card.
MCE_CARD = """MCE Light Green Residential Rates
(Rates effective 1.1.23)
E1, EM, ES, ESR, ET - Basic Residential
$0.149/kWh
ETOUC - Default Residential Time-of-Use
Summer - Service June 1 through September 30
Peak $0.195/kWh 4 P.M. to 9 P.M. every day
Off Peak $0.144/kWh All other hours
Winter - Service October 1 through May 31
Peak $0.149/kWh 4 P.M. to 9 P.M. every day
Off Peak $0.135/kWh All other hours
ETOUD - Residential Time-of-Use
Summer - Service June 1 through September 30
Peak $0.224/kWh 5 P.M. to 8 P.M. Monday through Friday
Off Peak $0.124/kWh All other hours including holidays**
Winter - Service October 1 through May 31
Peak $0.185/kWh 5 P.M. to 8 P.M. Monday through Friday
Off Peak $0.152/kWh All other hours including holidays**
ELEC - Residential Time-of-Use for Qualified Eletric Technologies
Summer - Service June 1 through September 30
Peak $0.301/kWh 4 P.M. to 9 P.M. every day
Part-Peak $0.199/kWh 3 P.M. to 4 P.M. and 9 P.M. to 12 A.M. every day
Off Peak $0.152/kWh All other hours
Winter - Service October 1 through May 31
Peak $0.134/kWh 4 P.M. to 9 P.M. every day
Part-Peak $0.113/kWh 3 P.M. to 4 P.M. and 9 P.M. to 12 A.M. every day
Off Peak $0.099/kWh All other hours
CLOSED rates
ETOUB - Residential Time-of-Use (Closed to new enrollments)
Summer - Service June 1 through September 30
Peak $0.999/kWh 4 P.M. to 9 P.M. every day
Off Peak $0.888/kWh All other hours
"""

ALIASES = {"ELEC": "eelec", "ETOUC": "etouc", "EV2": "ev2a"}


class TestCcaRateCard:
    def extract(self) -> tuple[dict, list[str]]:
        return cca.extract_generation([sheet(MCE_CARD)], ALIASES)

    def test_periods_are_read_per_schedule_and_season(self) -> None:
        generation, _ = self.extract()
        assert generation["eelec"]["summer"] == {
            "peak": 0.301,
            "part_peak": 0.199,
            "off_peak": 0.152,
        }
        assert generation["etouc"]["winter"] == {"peak": 0.149, "off_peak": 0.135}

    def test_a_schedule_with_no_part_peak_gets_none(self) -> None:
        generation, _ = self.extract()
        assert "part_peak" not in generation["etouc"]["summer"]

    def test_unvendored_schedules_are_skipped_and_named(self) -> None:
        # Silently dropping them would hide a schedule becoming relevant.
        _, skipped = self.extract()
        assert "ETOUD" in skipped

    def test_closed_schedules_are_not_priced(self) -> None:
        # Closed to new enrolment; including them would overwrite a live rate.
        generation, skipped = self.extract()
        assert not any(
            0.9 < r < 1.0 for s in generation.values() for p in s.values() for r in p.values()
        )
        assert "ETOUB" not in skipped

    def test_an_implausible_rate_is_an_error(self) -> None:
        broken = MCE_CARD.replace("Peak $0.195/kWh", "Peak $19.5/kWh")
        with pytest.raises(ExtractionError, match="plausible"):
            cca.extract_generation([sheet(broken)], ALIASES)

    def test_mismatched_period_sets_between_seasons_are_reported(self) -> None:
        generation, _ = self.extract()
        generation["etouc"]["winter"].pop("off_peak")
        assert "misread" in cca.verify(generation)[0]

    def test_the_card_dates_itself(self) -> None:
        assert sheets.parse_effective(MCE_CARD) == date(2023, 1, 1)


# Verbatim from PG&E Schedule NBT, ACC Plus table.
ACC_PLUS_TABLE = """Adopted Avoided Cost Calculator Plus Adder (ACC Plus)
Customer
Segment
2023
$/kWh
2024
$/kWh
2025
$/kWh
2026
$/kWh
2027
$/kWh
Residential 0.02200 0.01760 0.01320 0.00880 0.00440
Residential
Low
Income
0.09000 0.07200 0.05400 0.03600 0.01800
Non-
Residential
Not Eligible

The adder will decrease by 20 percent annually, for newly enrolled tariff
"""


class TestAccPlus:
    def test_both_customer_segments(self) -> None:
        got = accplus.extract([sheet(ACC_PLUS_TABLE)])
        assert got["residential"] == {
            2023: 0.02200,
            2024: 0.01760,
            2025: 0.01320,
            2026: 0.00880,
            2027: 0.00440,
        }
        assert got["residential_low_income"][2026] == 0.03600

    def test_the_longer_segment_name_wins(self) -> None:
        # "residential" is a substring of "residentiallowincome", so matching in
        # declaration order files the low-income row under residential and then
        # drops it as a duplicate.
        got = accplus.extract([sheet(ACC_PLUS_TABLE)])
        assert got["residential"][2023] != got["residential_low_income"][2023]

    def test_a_wrapped_segment_label_is_joined_to_its_figures(self) -> None:
        got = accplus.extract([sheet(ACC_PLUS_TABLE)])
        assert len(got["residential_low_income"]) == 5

    def test_trailing_prose_is_not_read_as_a_row(self) -> None:
        got = accplus.extract([sheet(ACC_PLUS_TABLE)])
        assert set(got) == {"residential", "residential_low_income"}

    def test_a_missing_segment_is_an_error(self) -> None:
        half = ACC_PLUS_TABLE.split("Residential\nLow")[0]
        with pytest.raises(ExtractionError, match="residential_low_income"):
            accplus.extract([sheet(half)])


class TestRefusesToGuess:
    def test_a_rider_that_differs_by_period_is_an_error(self) -> None:
        # [adders] holds one scalar per rider because they have always been
        # equal across periods. Taking the first column silently would misprice
        # the day that stops being true.
        broken = EELEC_UNBUNDLED.replace(
            "Transmission* (all usage) $0.04638  $0.04638  $0.04638",
            "Transmission* (all usage) $0.04638  $0.05000  $0.04638",
        )
        with pytest.raises(ExtractionError, match="differs by period"):
            rt.extract_unbundled(sheet(broken))

    def test_a_rider_equal_across_periods_is_fine(self) -> None:
        _, _, adders = rt.extract_unbundled(sheet(EELEC_UNBUNDLED))
        assert adders["transmission"] == 0.04638

    def test_a_sheet_with_no_advice_letter_is_refused(self) -> None:
        # Inheriting the previous snapshot's would make the emitted file claim a
        # revision it was not built from, which looks authoritative and is wrong.
        data = rt.Extracted(periods=["peak"])
        data.rates_effective = date(2026, 3, 1)
        with pytest.raises(ExtractionError, match="no advice letter"):
            rt.require_provenance(data)

    def test_a_sheet_with_an_advice_letter_passes(self) -> None:
        data = rt.Extracted(periods=["peak"])
        data.rates_advice = "7846-E"
        rt.require_provenance(data)


# Verbatim from PG&E Schedule E-FFS, Sheet 2.
EFFS_TABLE = """Customer Class DA/CCA Franchise Fee Surcharge Rate per kWh
Pre-2009 Vintage 2009 Vintage 2010 Vintage
Residential $0.00086 (R) $0.00064 (R) $0.00061 (R)
Small L&P $0.00083 (R) $0.00062 (R) $0.00059 (R)
Streetlights $0.00070 (R) $0.00052 (R) $0.00050 (R)
2011 Vintage 2012 Vintage 2013 Vintage
Residential $0.00060 (R) $0.00059 (R) $0.00059 (R)
Small L&P $0.00058 (R) $0.00057 (R) $0.00057 (R)
2014 Vintage 2015 Vintage 2016 Vintage
Residential $0.00059 (R) $0.00059 (R) $0.00059 (R)
2017 Vintage 2018 Vintage 2019 Vintage
Residential $0.00059 (R) $0.00059 (R) $0.00059 (R)
2020 Vintage 2021 Vintage 2022 Vintage
Residential $0.00059 (R) $0.00048 (R) $0.00048 (R)
"""


class TestFranchiseFees:
    def test_vintages_come_from_the_header_above_each_block(self) -> None:
        got = franchise.extract([sheet(EFFS_TABLE)])
        assert got[2009] == 0.00064
        assert got[2011] == 0.00060
        assert got[2021] == 0.00048

    def test_only_the_requested_customer_class_is_read(self) -> None:
        got = franchise.extract([sheet(EFFS_TABLE)])
        # Streetlights 2009 is 0.00052; residential is 0.00064.
        assert 0.00052 not in got.values()

    def test_the_pre_2009_vintage_is_dropped(self) -> None:
        # The PCIA table it is keyed alongside has no pre-2009 entry, so such a
        # customer cannot be priced anyway.
        got = franchise.extract([sheet(EFFS_TABLE)])
        assert 0.00086 not in got.values()
        assert all(isinstance(k, int) and k >= 2009 for k in got)

    def test_a_row_that_does_not_match_its_header_is_an_error(self) -> None:
        broken = EFFS_TABLE.replace(
            "Residential $0.00060 (R) $0.00059 (R) $0.00059 (R)",
            "Residential $0.00060 (R) $0.00059 (R)",
        )
        with pytest.raises(ExtractionError, match="values for"):
            franchise.extract([sheet(broken)])

    def test_a_truncated_table_is_an_error(self) -> None:
        half = EFFS_TABLE.split("2014 Vintage")[0]
        with pytest.raises(ExtractionError, match=r"only .* franchise fee vintages"):
            franchise.extract([sheet(half)])

    def test_a_document_without_the_table_is_an_error(self) -> None:
        with pytest.raises(ExtractionError, match="Franchise Fee"):
            franchise.extract([sheet("some other schedule entirely")])


# Verbatim from PG&E's AB920 rate table.
NSC_TABLE = """Net Surplus Compensation Rates for Energy
True-up Month NSC Rate* ($/kWh)
Jan. 2025 0.03396
Feb. 2025 0.03087
Mar. 2025 0.03043
Dec. 2025 0.03145
Jan. 2026 0.03116
July 2026 0.03089
Aug. 2026 0.02684
* Per D.11-06-016, the electricity portion of the NSC rate is the simple rolling
"""


class TestNscSeries:
    def test_months_are_keyed_year_first(self) -> None:
        got = nsc.extract([sheet(NSC_TABLE)])
        assert got["2025-01"] == 0.03396
        assert got["2026-08"] == 0.02684

    def test_a_full_month_name_parses_like_an_abbreviated_one(self) -> None:
        # The table mixes "Jan." with "July".
        got = nsc.extract([sheet(NSC_TABLE)])
        assert got["2026-07"] == 0.03089

    def test_the_footnote_is_not_read_as_a_row(self) -> None:
        got = nsc.extract([sheet(NSC_TABLE)])
        assert set(got) == {
            "2025-01",
            "2025-02",
            "2025-03",
            "2025-12",
            "2026-01",
            "2026-07",
            "2026-08",
        }

    def test_an_implausible_rate_is_an_error(self) -> None:
        broken = NSC_TABLE.replace("Jan. 2025 0.03396", "Jan. 2025 3.03396")
        with pytest.raises(ExtractionError, match="plausible"):
            nsc.extract([sheet(broken)])

    def test_a_truncated_series_is_an_error(self) -> None:
        half = NSC_TABLE.split("Mar. 2025")[0]
        with pytest.raises(ExtractionError, match=r"only .* NSC months"):
            nsc.extract([sheet(half)])

    def test_a_document_without_the_table_is_an_error(self) -> None:
        with pytest.raises(ExtractionError, match="Net Surplus"):
            nsc.extract([sheet("Jan. 2025 0.03396")])


class TestUnparseableCardIsStillWatched:
    """A card with no text layer cannot be rebuilt, but it can be watched.

    Detection was always the more valuable half of a scheduled check, and it
    needs only bytes -- so a republished card is noticed even when nothing can
    read it.
    """

    STORED = "0c0ff13a" * 8
    OTHER = "deadbeef" * 8

    def prev(self, **over: object) -> dict[str, object]:
        base = {"source_sha256": self.STORED, "source_read_on": "2026-08-12"}
        return {**base, **over}

    def test_matching_bytes_report_no_change(self) -> None:
        got = cca._watch_by_checksum(
            providers.MCE, self.STORED, self.prev(), ExtractionError("no text layer")
        )
        assert not got.changed and not got.failed
        assert "publisher has not moved" in got.messages[0]

    def test_different_bytes_report_a_change_and_say_what_to_do(self) -> None:
        got = cca._watch_by_checksum(
            providers.MCE, self.OTHER, self.prev(), ExtractionError("no text layer")
        )
        assert got.changed and not got.failed
        joined = " ".join(got.messages)
        assert "SOURCE CHANGED" in joined
        assert "re-read it from the rendered page" in joined
        assert self.OTHER in joined  # the checksum to record afterwards

    def test_a_change_is_not_a_failure(self) -> None:
        # It needs a human, but the run itself worked; failing would make the
        # scheduled job red until someone edits a file.
        got = cca._watch_by_checksum(
            providers.MCE, self.OTHER, self.prev(), ExtractionError("no text layer")
        )
        assert not got.failed

    def test_no_recorded_checksum_is_a_failure(self) -> None:
        # Without a baseline nothing can be detected at all, which is the one
        # state that must not look like "unchanged".
        got = cca._watch_by_checksum(
            providers.MCE, self.OTHER, {}, ExtractionError("no text layer")
        )
        assert got.failed
        assert "no source_sha256" in got.messages[0]

    def test_the_vendored_card_records_a_checksum(self) -> None:
        import tomllib

        raw = tomllib.loads(
            (
                Path(__file__).resolve().parent.parent
                / "src/nem_rates/data/cca/mce/2026-04-01.toml"
            ).read_text(encoding="utf-8")
        )
        assert len(str(raw.get("source_sha256", ""))) == 64
        assert raw.get("source_read_on")


PCIA_TABLE = """Vintage Power Charge Indifference Adjustment (per kWh) Rate
2009 Vintage $0.02973 (I)
2024 Vintage $0.05066 (I)
2025 Vintage ($0.01011) (I)
2026 Vintage ($0.01011) (N) (L)U 39Oakland, California
Revised Cal. P.U.C. Sheet No. 61121-E
"""


class TestPciaVintages:
    def test_the_last_row_is_read_even_when_the_page_header_runs_into_it(self) -> None:
        # "2026 Vintage ($0.01011) (N) (L)U 39Oakland, California" -- the figures
        # are not at end of line, and dropping the newest vintage is the worst
        # one to lose.
        got = rt.extract_pcia([sheet(PCIA_TABLE)])
        assert got[2026] == -0.01011

    def test_parenthesised_vintages_are_negative(self) -> None:
        got = rt.extract_pcia([sheet(PCIA_TABLE)])
        assert got[2025] == -0.01011
        assert got[2009] == 0.02973

    def test_a_sheet_without_the_table_yields_nothing(self) -> None:
        # Carried forward by the caller rather than invented here.
        assert rt.extract_pcia([sheet("no vintage table on this sheet")]) == {}


class TestScheduleNarrowing:
    def test_an_advice_letter_is_narrowed_to_one_schedule(self) -> None:
        pages = [
            sheet("ELECTRIC SCHEDULE E-ELEC Sheet 2\nSummer Usage $0.1 $0.2 $0.3"),
            sheet("ELECTRIC SCHEDULE E-TOU-C Sheet 2\nSummer (all usage) $0.4 $0.5"),
        ]
        assert len(rt.pages_for_schedule(pages, "E-ELEC")) == 1

    def test_a_schedule_the_filing_did_not_revise_says_what_it_carries(self) -> None:
        # A filing revises only some schedules, so this is a normal answer.
        pages = [sheet("ELECTRIC SCHEDULE E-TOU-C Sheet 2\nSummer (all usage) $0.4 $0.5")]
        with pytest.raises(ExtractionError, match="E-TOU-C"):
            rt.pages_for_schedule(pages, "EV2")


class TestBaseServicesChargeShapes:
    def test_the_tiered_form(self) -> None:
        got = rt.extract_base_services_charge([sheet(EELEC_TOTALS, number=2)])
        assert got == {"tier_1": 0.19713, "tier_2": 0.39688, "tier_3": 0.79343}

    def test_the_flat_pre_ab205_form_becomes_three_equal_tiers(self) -> None:
        # Before AB 205's charge began on 2026-03-01, E-ELEC had one flat rate
        # for everyone; recording it as equal tiers keeps consumers era-agnostic.
        flat = "TOTAL BUNDLED RATES\nBase Services Charge ($ per meter per day) $0.49281\n"
        assert rt.extract_base_services_charge([sheet(flat, number=2)]) == {
            "tier_1": 0.49281,
            "tier_2": 0.49281,
            "tier_3": 0.49281,
        }

    def test_absence_is_an_answer_not_a_failure(self) -> None:
        # E-TOU-C and EV2-A had no daily fixed charge at all before AB 205.
        assert rt.extract_base_services_charge([sheet("no charge table here")]) == {}


class TestVintagedTablesComeFromTheirOwnEra:
    """PCIA and the franchise fee are republished only when they change.

    Both live in documents with their own vintages -- the PCIA on the schedule's
    own sheet, the franchise fee in Schedule E-FFS -- and a filing that does not
    touch them simply omits them. Reading the current version for a historical
    snapshot put January 2026's values on a December 2025 cycle: 0.03492 against
    the 0.01161 billed for PCIA, 0.00060 against 0.00105 for the surcharge.
    """

    def vintage(self, slug: str, effective: str) -> dict:
        import tomllib

        path = (
            Path(__file__).resolve().parent.parent
            / f"src/nem_rates/data/tariff/pge/{slug}/{effective}.toml"
        )
        return tomllib.loads(path.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("slug", ["eelec", "etouc", "ev2a"])
    def test_the_2025_vintages_carry_the_2025_pcia(self, slug: str) -> None:
        for effective in ("2025-01-01", "2025-03-01", "2025-09-01"):
            raw = self.vintage(slug, effective)
            assert raw["cca"]["pcia_vintages"]["2011"] == pytest.approx(0.01161), effective

    @pytest.mark.parametrize("slug", ["eelec", "etouc", "ev2a"])
    def test_the_2026_vintages_carry_the_2026_pcia(self, slug: str) -> None:
        for effective in ("2026-01-01", "2026-03-01"):
            raw = self.vintage(slug, effective)
            assert raw["cca"]["pcia_vintages"]["2011"] == pytest.approx(0.03492), effective

    @pytest.mark.parametrize("slug", ["eelec", "etouc", "ev2a"])
    def test_the_franchise_fee_follows_its_own_schedule(self, slug: str) -> None:
        # Billed 0.00105 for the December 2025 segment, 0.00060 for January.
        assert self.vintage(slug, "2025-09-01")["cca"]["franchise_fee_vintages"][
            "2011"
        ] == pytest.approx(0.00105)
        assert self.vintage(slug, "2026-01-01")["cca"]["franchise_fee_vintages"][
            "2011"
        ] == pytest.approx(0.00060)


# Verbatim from CDTFA notice L-1020.
CDTFA_NOTICE = """DECEMBER 2025 L-1020
2026 Energy Resources (Electrical Energy) Surcharge Rate
The California Energy Commission (CEC) set the electrical energy surcharge rate
for the 2026 calendar year to remain at three-tenths mill ($.0003) per
kilowatt-hour. In the future, we will only send you a notice when the rate
changes.
"""


class TestEnergySurcharge:
    def test_the_year_and_rate_are_read(self) -> None:
        assert tax.extract([sheet(CDTFA_NOTICE)]) == (2026, 0.0003)

    def test_an_implausible_rate_is_an_error(self) -> None:
        broken = CDTFA_NOTICE.replace("$.0003", "$3.0")
        with pytest.raises(ExtractionError, match="plausible"):
            tax.extract([sheet(broken)])

    def test_a_reworded_notice_is_an_error_rather_than_a_guess(self) -> None:
        with pytest.raises(ExtractionError, match="expected form"):
            tax.extract([sheet("some other CDTFA notice entirely")])

    def test_the_rendered_file_is_loadable_as_a_vintage(self) -> None:
        body = tax.render(providers.CA_ENERGY_RESOURCES, 2026, 0.0003, "L-1020")
        assert tax.verify_against_library(body, 2026, 0.0003) == []

    def test_a_rendered_file_that_disagrees_is_caught(self) -> None:
        body = tax.render(providers.CA_ENERGY_RESOURCES, 2026, 0.0003, "L-1020")
        assert tax.verify_against_library(body, 2026, 0.0004) != []

    @pytest.mark.parametrize("year", [2025, 2026])
    def test_both_vintages_are_vendored(self, year: int) -> None:
        # CDTFA issues a notice only when the rate changes, so the notices are
        # exactly the vintages that exist.
        import tomllib

        path = (
            Path(__file__).resolve().parent.parent
            / f"src/nem_rates/data/tax/ca_energy_resources/{year}-01-01.toml"
        )
        assert tomllib.loads(path.read_text(encoding="utf-8"))["rate"] == pytest.approx(0.0003)


class TestFilingScanWidening:
    """Reaching back for a date older than the default scan.

    The caller knows the date it wants priced; it has no way to know which
    advice letter numbers that lands on, which is the whole reason `--for-date`
    exists. So a date that falls outside the default range has to widen on its
    own rather than telling the user to guess a number range.
    """

    def _stub(
        self, monkeypatch: pytest.MonkeyPatch, *, target: int | None
    ) -> list[tuple[int, int, bool]]:
        """Stand in for the network. Returns the (lo, hi, refresh) of each pass."""
        from nem_rates.regen import filings

        passes: list[tuple[int, int, bool]] = []
        index: dict[str, filings.Filing] = {}

        def build_index(
            util: object,
            lo: int,
            hi: int,
            root: object,
            *,
            refresh: bool = False,
            report: object = print,
        ) -> dict[str, filings.Filing]:
            passes.append((lo, hi, refresh))
            if refresh:
                index.clear()
            for number in range(lo, hi + 1):
                index[f"{number}-E"] = filings.Filing(f"{number}-E", 0, {})
            return dict(index)

        def filing_for(
            util: object, sheet: str, on: date, indexed: dict[str, filings.Filing]
        ) -> filings.Filing | None:
            if target is None or f"{target}-E" not in indexed:
                return None
            return filings.Filing(f"{target}-E", 0, {sheet.upper(): "2024-01-01"})

        monkeypatch.setattr(filings, "load_index", lambda root, key: {})
        monkeypatch.setattr(filings, "build_index", build_index)
        monkeypatch.setattr(filings, "filing_for", filing_for)
        return passes

    def test_it_reaches_back_until_it_finds_the_filing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from nem_rates import regen

        passes = self._stub(monkeypatch, target=7250)
        found = regen._filing_for_date("eelec", date(2024, 6, 1), tmp_path, None, False)

        assert found == "7250-E"
        # Contiguous and disjoint, walking backwards from the default range.
        assert [(lo, hi) for lo, hi, _ in passes] == [(7500, 7900), (7300, 7499), (7100, 7299)]

    def test_it_stops_at_the_bound_and_says_how_far_it_got(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A date the utility never filed for has to stop somewhere rather than
        # walking back to advice letter one.
        from nem_rates import regen

        passes = self._stub(monkeypatch, target=None)
        with pytest.raises(ExtractionError) as caught:
            regen._filing_for_date("eelec", date(2015, 1, 1), tmp_path, None, False)

        assert len(passes) == 1 + regen.MAX_SCAN_WIDENINGS
        message = str(caught.value)
        assert (
            f"reached back to {regen.DEFAULT_SCAN[0] - regen.MAX_SCAN_WIDENINGS * regen.SCAN_STEP}"
            in message
        )
        assert f"{regen.MAX_SCAN_WIDENINGS} widening(s)" in message
        assert "--scan" in message

    def test_an_explicit_scan_is_honoured_rather_than_widened_past(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Passing --scan is the caller pinning a range; searching outside it
        # anyway would defeat the point of having passed it.
        from nem_rates import regen

        passes = self._stub(monkeypatch, target=None)
        with pytest.raises(ExtractionError, match="pinned to 7000-7100"):
            regen._filing_for_date("eelec", date(2015, 1, 1), tmp_path, (7000, 7100), False)

        assert [(lo, hi) for lo, hi, _ in passes] == [(7000, 7100)]

    def test_refresh_applies_only_to_the_first_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A widening probes numbers the index has never held, so re-fetching
        # with refresh set would discard the block just indexed instead of
        # adding to it -- and the search would never accumulate enough to hit.
        from nem_rates import regen

        passes = self._stub(monkeypatch, target=7250)
        regen._filing_for_date("eelec", date(2024, 6, 1), tmp_path, None, True)

        assert [refresh for _, _, refresh in passes] == [True, False, False]


class TestRegistryOmissions:
    """A provider registered without the document that publishes its rate."""

    def test_a_tax_with_no_notices_says_what_is_missing(self) -> None:
        # `regen tax` falls back to latest_notice when no --notice is passed, so
        # a bare IndexError here would surface far from the omission causing it.
        bare = providers.Tax(
            key="nowhere",
            name="A surcharge nobody filed a notice for",
            jurisdiction="XX",
            notice_url="https://example.invalid/{notice}.pdf",
        )
        with pytest.raises(ExtractionError) as caught:
            _ = bare.latest_notice
        message = str(caught.value)
        assert "lists no notices" in message
        assert "--notice" in message

    def test_a_registered_tax_still_resolves_its_latest(self) -> None:
        assert providers.CA_ENERGY_RESOURCES.latest_notice == "L-1020"
