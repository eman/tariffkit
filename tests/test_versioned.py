"""Effective-dated data resolution.

The rule is the same for every dataset that has vintages, so it is tested once
here rather than per consumer: the version in force is the latest one effective
on or before the date, and a date before every vintage raises instead of
borrowing the earliest.

That last behaviour is the point of the whole mechanism. Reaching backwards
silently would price a period with rates that had not been adopted yet, and the
result looks entirely plausible -- PG&E's public purpose programs charge moved
by more than two cents a kilowatt-hour between January and March 2026, and a
January bill priced at March rates gives no sign of it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from nem_rates.data import versioned
from nem_rates.errors import DataError

TARIFF = "tariff/pge/eelec"
CARD = "cca/mce"
ADDER = "export/pge/acc_plus"


class TestResolution:
    def test_the_latest_version_not_in_the_future_wins(self) -> None:
        got = versioned.load(TARIFF, date(2026, 7, 15))
        assert got.effective <= date(2026, 7, 15)
        assert got.effective == max(v.effective for v in versioned.versions(TARIFF))

    def test_the_effective_date_itself_resolves(self) -> None:
        earliest = versioned.versions(TARIFF)[0].effective
        assert versioned.load(TARIFF, earliest).effective == earliest

    def test_a_date_before_every_vintage_raises(self) -> None:
        earliest = versioned.versions(TARIFF)[0].effective
        before = date(earliest.year - 1, 1, 1)
        with pytest.raises(DataError) as caught:
            versioned.load(TARIFF, before)
        assert "no version effective on or before" in str(caught.value)

    def test_the_error_says_what_to_do_about_it(self) -> None:
        # The fix is to vendor the missing vintage, not to widen the lookup, so
        # the message names the earliest one and says so.
        with pytest.raises(DataError) as caught:
            versioned.load(TARIFF, date(2000, 1, 1))
        message = str(caught.value)
        assert "earliest vendored is" in message
        assert "Vendor the vintage covering" in message

    def test_an_unknown_dataset_raises(self) -> None:
        with pytest.raises(DataError, match="no vendored data at"):
            versioned.load("cca/nosuchprovider", date(2026, 7, 15))

    def test_versions_come_back_oldest_first(self) -> None:
        found = versioned.versions(TARIFF)
        assert [v.effective for v in found] == sorted(v.effective for v in found)


class TestEveryVersionedDataset:
    """Each dataset resolving by date must actually be resolvable by date."""

    @pytest.mark.parametrize("dataset", [TARIFF, CARD, ADDER])
    def test_has_at_least_one_vintage(self, dataset: str) -> None:
        assert versioned.versions(dataset)

    @pytest.mark.parametrize("dataset", [TARIFF, CARD, ADDER])
    def test_every_vintage_declares_its_effective_date(self, dataset: str) -> None:
        # A file with no date cannot be placed in time, so it would have to be
        # assumed current -- the assumption this module exists to remove.
        for version in versioned.versions(dataset):
            assert isinstance(version.effective, date)
            assert version.raw["effective"] == version.effective.isoformat()

    @pytest.mark.parametrize("dataset", [TARIFF, CARD, ADDER])
    def test_the_filename_matches_the_declared_date(self, dataset: str) -> None:
        # Otherwise the directory listing lies about what is vendored.
        from importlib.resources import files

        root = files("nem_rates.data")
        for part in dataset.split("/"):
            root = root / part
        names = sorted(e.name for e in root.iterdir() if e.name.endswith(".toml"))
        assert names == sorted(
            f"{v.effective.isoformat()}.toml" for v in versioned.versions(dataset)
        )

    @pytest.mark.parametrize("dataset", [TARIFF, CARD, ADDER])
    def test_coverage_reports_the_span(self, dataset: str) -> None:
        lo, hi = versioned.coverage(dataset)
        assert lo <= hi


class TestCcaCardsResolveByDate:
    def test_a_card_is_chosen_for_the_moment_being_priced(self) -> None:
        from nem_rates.cca import load_rate_card

        card = load_rate_card("mce", date(2026, 7, 15))
        assert card.provider == "MCE"

    def test_a_date_before_the_earliest_card_raises_rather_than_guessing(self) -> None:
        # Asks the data where its own edge is: vintages get backfilled, so a
        # hardcoded date here goes stale the moment one lands before it.
        from nem_rates.cca import load_rate_card

        earliest = versioned.versions(CARD)[0].effective
        with pytest.raises(DataError, match="no version effective on or before"):
            load_rate_card("mce", earliest - timedelta(days=1))

    def test_january_2026_resolves_to_the_card_that_was_in_force(self) -> None:
        # MCE repriced on 2026-04-01, moving down to parity with PG&E. Pricing
        # January with April's card understated generation by about two cents a
        # kilowatt-hour; the December 2025 statement shows 0.14900/0.13500 for
        # E-TOU-C winter, which is what the vintage in force must give.
        from nem_rates.cca import load_rate_card

        card = load_rate_card("mce", date(2026, 1, 15))
        assert card.generation("E-TOU-C", "winter", "peak") == pytest.approx(0.14900)
        assert card.generation("E-TOU-C", "winter", "off_peak") == pytest.approx(0.13500)

    def test_july_2026_resolves_to_the_repriced_card(self) -> None:
        from nem_rates.cca import load_rate_card

        card = load_rate_card("mce", date(2026, 7, 15))
        assert card.generation("E-TOU-C", "winter", "peak") == pytest.approx(0.13710)

    def test_an_unvendored_provider_says_to_supply_rates_instead(self) -> None:
        # A different problem from "we have it but not that far back", and it
        # has a different answer, so it keeps its own message.
        from nem_rates.cca import load_rate_card

        with pytest.raises(DataError, match="supply rates via"):
            load_rate_card("nosuchcca", date(2026, 7, 15))
