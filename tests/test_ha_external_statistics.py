"""Nothing assumes a statistic belongs to an entity.

Integrations that import history rather than publish live sensors -- a utility
feed such as opower is the obvious one -- write *external* statistics, whose
ids are `source:object` rather than `domain.object`. There is no support for
any particular one of those here and none is planned. What is pinned is that
none is shut out: the reading path takes statistic ids, and so do both of the
ways an id gets configured.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from tests.test_ha_energy import NOW, _entry, _meter_options, _setup, _state

from tariffkit.timeutil import PACIFIC

IMPORT_STAT = "opower:utility_elec_probe_energy_consumption"
EXPORT_STAT = "opower:utility_elec_probe_energy_return"


async def _record_external(
    hass: HomeAssistant, statistic_id: str, points: list[tuple[datetime, float]]
) -> None:
    metadata: dict[str, Any] = {
        "mean_type": StatisticMeanType.NONE,
        "has_mean": False,
        "has_sum": True,
        "name": None,
        # Not "recorder": that is what makes this external rather than an
        # entity's own compiled history.
        "source": "opower",
        "statistic_id": statistic_id,
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }
    async_add_external_statistics(
        hass,
        metadata,
        [
            {"start": start.astimezone(PACIFIC), "state": total, "sum": total}
            for start, total in points
        ],
    )
    await async_wait_recording_done(hass)


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_a_statistic_with_no_entity_behind_it_still_prices(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    seed = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    await _record_external(
        hass, IMPORT_STAT, [(seed + timedelta(hours=n), 1000.0 + n * 2.0) for n in range(14)]
    )
    await _record_external(
        hass, EXPORT_STAT, [(seed + timedelta(hours=n), 500.0 + n * 1.0) for n in range(14)]
    )

    entry = _entry(_meter_options(grid_import_entity=IMPORT_STAT, grid_export_entity=EXPORT_STAT))
    await _setup(hass, entry)

    # 13 completed hours of 2.0 and 1.0. The first row is the counter's own
    # baseline and has nothing to difference against, so it is not billed.
    assert float(_state(hass, entry, "grid_import_cycle").state) == pytest.approx(26.0)
    assert float(_state(hass, entry, "grid_export_cycle").state) == pytest.approx(13.0)
    assert float(_state(hass, entry, "amount_due_cycle").state) > 0


def test_the_options_form_accepts_a_statistic_id() -> None:
    """The form is a statistic picker, so it takes ids with no entity behind them.

    It was an `EntitySelector`, which validates `domain.object` and refused
    `source:object` outright -- so the reading path handled a statistic the
    form could not be told about. The cost of the swap is that the picker lists
    every statistic rather than only energy sensors; `_meter_problem` is what
    keeps the answer honest.
    """
    from custom_components.tariffkit.config_flow import _meters_schema

    schema = _meters_schema({})
    assert schema({"grid_import_entity": "sensor.grid_import_total"})
    assert schema({"grid_import_entity": IMPORT_STAT})


def test_an_id_that_is_neither_shape_is_refused() -> None:
    """Widening the validator must not make it accept a typo."""
    from custom_components.tariffkit.config_flow import _meter_problem

    problem = _meter_problem(None, {"grid_import_entity": "grid_import_total"})  # type: ignore[arg-type]
    assert "neither an entity id" in problem


def test_the_profile_carries_a_statistic_id_too() -> None:
    """The CLI-side mapping is the other way these ids reach the integration."""
    from tariffkit.account.model import MeterSource

    source = MeterSource(IMPORT_STAT, EXPORT_STAT)
    assert source.grid_import_entity == IMPORT_STAT


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_a_statistic_that_is_not_energy_is_refused(hass: HomeAssistant) -> None:
    """A picker that lists every statistic can offer a gas meter.

    The recorder converts only within a unit class, so asking for kWh yields no
    converter for cubic metres and the raw volume is billed as energy: 26 m3 of
    gas priced as a $26.59 electricity bill before this check existed.
    """
    from custom_components.tariffkit.config_flow import _async_meter_problem

    gas = "opower:utility_gas_probe_volume"
    async_add_external_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_mean": False,
            "has_sum": True,
            "name": None,
            "source": "opower",
            "statistic_id": gas,
            "unit_class": "volume",
            "unit_of_measurement": "m³",
        },
        [{"start": NOW.replace(hour=0, minute=0), "state": 1.0, "sum": 1.0}],
    )
    await async_wait_recording_done(hass)

    problem = await _async_meter_problem(hass, {"grid_import_entity": gas})
    assert "not energy" in problem
    assert "volume" in problem

    # An energy statistic in a different energy unit is fine: the recorder does
    # convert within the class, so Wh is not a problem to refuse.
    await _record_external(hass, IMPORT_STAT, [(NOW.replace(hour=0, minute=0), 1.0)])
    assert await _async_meter_problem(hass, {"grid_import_entity": IMPORT_STAT}) == ""


def test_a_statistic_id_is_refused_for_influxdb() -> None:
    """Statistic ids are a Home Assistant idea; InfluxDB is queried by series name.

    The series name goes into SQL and is checked against a stricter pattern at
    query time, so accepting a colon on the profile only stored something that
    failed later with a message about the wrong thing.
    """
    from tariffkit.account.model import MeterSource, MeterSources

    assert MeterSources(ha=MeterSource(IMPORT_STAT, EXPORT_STAT)).ha is not None
    with pytest.raises(Exception, match="may not contain a colon"):
        MeterSources(influx=MeterSource(IMPORT_STAT, EXPORT_STAT))


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_a_statistic_with_no_running_sum_is_refused(hass: HomeAssistant) -> None:
    """The statistic equivalent of the state_class check.

    An hourly `change` only exists for a summed statistic, so a mean-only one
    has nothing to difference and would price every hour as zero -- the same
    confident nonsense a `measurement` sensor produces, which the entity path
    has always refused.
    """
    from custom_components.tariffkit.config_flow import _async_meter_problem

    mean_only = "opower:utility_elec_probe_demand"
    async_add_external_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_mean": True,
            "has_sum": False,
            "name": None,
            "source": "opower",
            "statistic_id": mean_only,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        [{"start": NOW.replace(hour=0, minute=0), "mean": 1.0, "min": 1.0, "max": 1.0}],
    )
    await async_wait_recording_done(hass)

    problem = await _async_meter_problem(hass, {"grid_import_entity": mean_only})
    assert "no running sum" in problem
