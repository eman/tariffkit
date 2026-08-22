"""Component grouping: the roll-up a stacked chart is drawn from.

The property that matters is the sum identity. A stacked chart claims that its
bands *are* the price; if a component were dropped, mapped into a group the
direction does not draw, or double-counted, the stack would silently disagree
with the Import Price entity sitting next to it on the same dashboard.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from _pytest.mark import ParameterSet

from tariffkit import CcaConfig, Config, RateEngine, Supplier
from tariffkit.components import (
    COMPONENT_GROUPS,
    EXPORT_GROUPS,
    IMPORT_GROUPS,
    ComponentGroup,
    group_components,
    group_of,
    split_components,
)
from tariffkit.tariff.retail import SUPPORTED_TARIFFS
from tariffkit.timeutil import PACIFIC

#: One winter and one summer day, covering peak, part-peak and off-peak hours.
#: Both fall inside every vendored schedule's effective window; the earliest
#: snapshot some schedules carry is 2026-03-01.
DAYS = (date(2026, 12, 14), date(2026, 8, 12))


def configs() -> list[ParameterSet]:
    """The account shapes that produce structurally different component sets."""
    event = date(2026, 8, 12)
    cases: list[tuple[str, Config]] = [
        *((f"bundled-{tariff}", Config(tariff=tariff)) for tariff in SUPPORTED_TARIFFS),
        (
            "care",
            Config(
                tariff="E-ELEC",
                discount="care",
                acc_plus_segment="residential_low_income",
            ),
        ),
        (
            "fera",
            Config(
                tariff="E-TOU-C",
                discount="fera",
                acc_plus_segment="residential_low_income",
            ),
        ),
        ("medical", Config(tariff="E-ELEC", medical_baseline=True, medical_kwh_per_day=16.5)),
        (
            "cca",
            Config(
                tariff="E-ELEC",
                supplier=Supplier.CCA,
                cca=CcaConfig(name="MCE", rate_card="mce", pcia_vintage=2021),
            ),
        ),
        (
            "smartrate",
            Config(
                tariff="E-ELEC",
                smartrate=True,
                smartrate_events=(event,),
                smartrate_known_through=event,
            ),
        ),
    ]
    return [pytest.param(config, id=label) for label, config in cases]


@pytest.mark.parametrize("config", configs())
def test_groups_sum_to_the_price(config: Config) -> None:
    """Every hour of every account shape: the bands reconstruct the price."""
    engine = RateEngine(config)
    for day in DAYS:
        for hour in range(24):
            point = engine.price_at(datetime(day.year, day.month, day.day, hour, tzinfo=PACIFIC))
            import_groups = point.import_price.grouped()
            export_groups = point.export_price.grouped()
            assert sum(import_groups.values()) == pytest.approx(
                point.import_price.total, abs=5e-6
            ), f"{day} {hour}:00 import"
            assert sum(export_groups.values()) == pytest.approx(
                point.export_price.total, abs=5e-6
            ), f"{day} {hour}:00 export"


@pytest.mark.parametrize("config", configs())
def test_group_sets_do_not_vary_with_the_account(config: Config) -> None:
    """A dashboard's series list survives a CCA, a discount, or a rate change."""
    point = RateEngine(config).price_at(datetime(2026, 8, 12, 18, tzinfo=PACIFIC))
    assert tuple(point.import_price.grouped()) == IMPORT_GROUPS
    assert tuple(point.export_price.grouped()) == EXPORT_GROUPS


@pytest.mark.parametrize("config", configs())
def test_nothing_real_lands_in_other(config: Config) -> None:
    """``OTHER`` is a safety valve, not a bucket the vendored data uses.

    A non-zero ``other`` band on vendored data means a schedule grew a line and
    ``COMPONENT_GROUPS`` has not caught up -- which is exactly the thing this
    test exists to notice before a chart does.
    """
    point = RateEngine(config).price_at(datetime(2026, 8, 12, 18, tzinfo=PACIFIC))
    assert point.import_price.grouped()[ComponentGroup.OTHER] == 0.0
    assert point.export_price.grouped()[ComponentGroup.OTHER] == 0.0


def test_unknown_components_are_kept_not_dropped() -> None:
    """An unrecognized line still counts toward the total, via ``OTHER``."""
    components = {"generation": 0.2, "some_new_2027_rider": 0.05}
    grouped = group_components(components, IMPORT_GROUPS)
    assert grouped[ComponentGroup.GENERATION] == pytest.approx(0.2)
    assert grouped[ComponentGroup.OTHER] == pytest.approx(0.05)
    assert sum(grouped.values()) == pytest.approx(sum(components.values()))


def test_a_group_outside_the_direction_folds_into_other() -> None:
    """Export's delivery band has no import equivalent, so it cannot vanish."""
    grouped = group_components({"delivery": 0.03}, IMPORT_GROUPS)
    assert grouped[ComponentGroup.OTHER] == pytest.approx(0.03)
    assert sum(grouped.values()) == pytest.approx(0.03)


def test_discount_components_are_matched_by_suffix() -> None:
    """CARE and FERA name their component for the program, so match the suffix."""
    assert group_of("care_discount") is ComponentGroup.CREDITS
    assert group_of("fera_discount") is ComponentGroup.CREDITS
    assert group_of("some_future_discount") is ComponentGroup.CREDITS


def test_split_keeps_the_lines_behind_each_band() -> None:
    price = RateEngine(Config(tariff="E-ELEC")).price_at(datetime(2026, 8, 12, 18, tzinfo=PACIFIC))
    split = split_components(price.import_price.components, IMPORT_GROUPS)
    assert set(split) == set(IMPORT_GROUPS)
    assert "generation" in split[ComponentGroup.GENERATION]
    assert "wildfire_fund_charge" in split[ComponentGroup.SURCHARGES]
    # Every line appears exactly once across the bands.
    assigned = [name for lines in split.values() for name in lines]
    assert sorted(assigned) == sorted(price.import_price.components)


def test_every_group_has_a_label() -> None:
    """MQTT and CLI have no translation files to fall back on."""
    assert all(group.label for group in ComponentGroup)
    assert set(COMPONENT_GROUPS.values()) <= set(ComponentGroup)


def test_to_dict_carries_the_groups() -> None:
    """The web API and the MQTT attribute payloads ride on ``to_dict``."""
    point = RateEngine(Config(tariff="E-ELEC")).price_at(datetime(2026, 8, 12, 18, tzinfo=PACIFIC))
    payload = point.to_dict()
    assert set(payload["import"]["groups"]) == {str(group) for group in IMPORT_GROUPS}
    assert set(payload["export"]["groups"]) == {str(group) for group in EXPORT_GROUPS}
    assert sum(payload["import"]["groups"].values()) == pytest.approx(
        payload["import"]["total"], abs=5e-6
    )
