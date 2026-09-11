"""Nothing in the reading path assumes a statistic belongs to an entity.

Integrations that import history rather than publish live sensors -- a utility
feed such as opower is the obvious one -- write *external* statistics, whose
ids are `source:object` rather than `domain.object`. There is no support for
any particular one of those here and none is planned. This pins the weaker
thing worth keeping: that the code reads a statistic id, not an entity, so
such a feed is not structurally shut out.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
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


def test_the_options_picker_is_what_would_stand_in_the_way() -> None:
    """The reading path is open; the configuration path is not.

    `EntitySelector` validates an entity id, and `source:object` is not one, so
    an external statistic cannot be typed into the meters form even though
    everything downstream would handle it. Recorded rather than fixed: no such
    feed is supported today, and the fix is a decision about the form, not an
    accident to be patched.
    """
    energy = selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=selector.EntityWithDeviceFilterSelectorConfig(
                domain="sensor", device_class="energy"
            )
        )
    )
    schema = vol.Schema({vol.Optional("grid_import_entity"): energy})

    assert schema({"grid_import_entity": "sensor.grid_import_total"})
    with pytest.raises(vol.Invalid):
        schema({"grid_import_entity": IMPORT_STAT})
