"""Pricing metered history into long-term statistics."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from custom_components.tariffkit import backfill
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from tariffkit import Config
from tariffkit.account import AccountEpoch, AccountProfile
from tariffkit.billing import BillingPeriod, IntervalReading
from tariffkit.timeutil import PACIFIC


def _profile(tariff: str = "E-ELEC", **kwargs: object) -> AccountProfile:
    config = Config(tariff=tariff, pto_date=date(2026, 1, 1), **kwargs)  # type: ignore[arg-type]
    return AccountProfile((AccountEpoch(date(2026, 1, 1), config),), name="probe")


def _hours(
    opens: date, closes: date, imported: float, exported: float = 0.0
) -> list[IntervalReading]:
    out, day = [], opens
    while day <= closes:
        for hour in range(24):
            out.append(
                IntervalReading(
                    datetime(day.year, day.month, day.day, hour, tzinfo=PACIFIC),
                    imported=imported,
                    exported=exported,
                )
            )
        day += timedelta(days=1)
    return out


def test_days_sum_to_the_cycle_that_contains_them() -> None:
    """The decomposition's whole justification: it must reconstruct the bill.

    Daily figures are marginal contributions, so a rounding-free sum of them has
    to equal the cycle priced in one go. If it does not, the history and the
    statement disagree and the history is the one that is wrong.
    """
    from custom_components.tariffkit.energy import price

    profile = _profile()
    cycle = BillingPeriod(date(2026, 7, 1), date(2026, 7, 20))
    readings = _hours(cycle.start, cycle.end, imported=0.5, exported=0.2)

    days, reason, _ = backfill.price_cycle(profile, readings, cycle)
    assert reason == ""
    assert len(days) == 20

    whole, _ = price(profile, readings, cycle)
    assert whole is not None
    assert sum(d.net_cost for d in days) == pytest.approx(whole.total, abs=1e-9)
    assert sum(d.energy_cost for d in days) == pytest.approx(
        whole.energy_charges + whole.taxes, abs=1e-9
    )
    assert sum(d.export_credit for d in days) == pytest.approx(-whole.export_credits, abs=1e-9)
    assert sum(d.grid_import for d in days) == pytest.approx(0.5 * 24 * 20)


def test_a_baseline_schedule_still_decomposes_exactly() -> None:
    """Where pricing a day alone would be wrong, differencing still is not.

    E-TOU-C's baseline allowance is cycle-cumulative, which is what made a
    standalone one-day bill overstate a heavy day. The marginal decomposition
    has to stay exact on precisely that schedule.
    """
    from custom_components.tariffkit.energy import price

    profile = _profile("E-TOU-C", baseline_territory="X")
    cycle = BillingPeriod(date(2026, 7, 1), date(2026, 7, 15))
    readings = _hours(cycle.start, cycle.end, imported=1.5)
    readings.append(IntervalReading(datetime(2026, 7, 9, 2, tzinfo=PACIFIC), imported=60.0))

    days, reason, _ = backfill.price_cycle(profile, readings, cycle)
    assert reason == ""
    whole, _ = price(profile, readings, cycle)
    assert whole is not None
    assert sum(d.net_cost for d in days) == pytest.approx(whole.total, abs=1e-9)
    # And no single day exceeds the cycle it belongs to.
    assert max(d.net_cost for d in days) < whole.total


def test_statistics_carry_a_running_sum_and_the_days_own_value() -> None:
    profile = _profile()
    cycle = BillingPeriod(date(2026, 7, 1), date(2026, 7, 3))
    readings = _hours(cycle.start, cycle.end, imported=1.0)
    result = backfill.build(profile, readings, cycle.start, cycle.end, 1)

    series = next(s for s in backfill.SERIES if s.slug == "grid_import")
    rows = backfill.statistics_for(result, series)
    assert [r["state"] for r in rows] == [pytest.approx(24.0)] * 3
    assert [r["sum"] for r in rows] == [
        pytest.approx(24.0),
        pytest.approx(48.0),
        pytest.approx(72.0),
    ]
    assert rows[0]["start"] == datetime(2026, 7, 1, tzinfo=PACIFIC)
    assert series.statistic_id("probe") == "tariffkit:probe_grid_import"


def test_a_cycle_the_history_only_partly_covers_is_skipped_whole() -> None:
    """Partly-covered cycles are refused; later complete ones are not.

    A cycle the account history joins halfway through cannot be decomposed: the
    cycle-to-date bills before the epoch do not exist, so the days after it have
    nothing to be marginal *to*. Pricing them as though the cycle began at the
    epoch would under-grant a baseline allowance the real cycle had been banking
    since its true start. One cycle is lost and said so; the rest are priced.
    """
    profile = AccountProfile(
        (AccountEpoch(date(2026, 7, 10), Config(tariff="E-ELEC")),), name="late"
    )
    readings = _hours(date(2026, 6, 1), date(2026, 8, 20), imported=1.0)
    result = backfill.build(profile, readings, date(2026, 6, 1), date(2026, 8, 20), 1)

    assert len(result.skipped) == 2, "June, and the July cycle the epoch lands inside"
    assert any("2026-07-01..2026-07-31" in s for s in result.skipped)
    assert result.days, "August is wholly covered and must still be priced"
    assert min(d.day for d in result.days) == date(2026, 8, 1)
    assert max(d.day for d in result.days) == date(2026, 8, 20)


def test_cycles_follow_statement_evidence_where_it_exists() -> None:
    profile = _profile()
    cycles = backfill.cycles_between(profile, date(2026, 5, 15), date(2026, 7, 20), 1)
    assert [(c.start, c.end) for c in cycles] == [
        (date(2026, 5, 15), date(2026, 5, 31)),
        (date(2026, 6, 1), date(2026, 6, 30)),
        (date(2026, 7, 1), date(2026, 7, 20)),
    ]
    # The window's own edges clip the first and last cycle rather than
    # reaching outside what was asked for.
    assert cycles[0].start == date(2026, 5, 15)
    assert cycles[-1].end == date(2026, 7, 20)


IMPORT_ENTITY = "sensor.grid_energy_delivered"
EXPORT_ENTITY = "sensor.grid_energy_received"
NOW = datetime(2026, 7, 21, 9, 0, tzinfo=PACIFIC)


def _meta(entity: str) -> dict[str, object]:
    from homeassistant.components.recorder.models import StatisticMeanType

    return {
        "mean_type": StatisticMeanType.NONE,
        "has_mean": False,
        "has_sum": True,
        "name": None,
        "source": "recorder",
        "statistic_id": entity,
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_the_action_writes_external_statistics(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """End to end: seeded meter statistics in, priced history out."""
    import json

    from custom_components.tariffkit.const import (
        CONF_CYCLE_START_DAY,
        CONF_FORECAST_HOURS,
        CONF_GRID_EXPORT_ENTITY,
        CONF_GRID_IMPORT_ENTITY,
        CONF_PROFILE,
        DOMAIN,
    )
    from homeassistant.components.recorder.statistics import (
        async_import_statistics,
        statistics_during_period,
    )
    from homeassistant.helpers.recorder import get_instance
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    freezer.move_to(NOW)
    profile = _profile()
    # One kWh imported and a quarter exported, every hour of a fortnight.
    for entity, per_hour in ((IMPORT_ENTITY, 1.0), (EXPORT_ENTITY, 0.25)):
        rows, total = [], 0.0
        start = datetime(2026, 7, 1, tzinfo=PACIFIC) - timedelta(hours=1)
        for offset in range(24 * 20 + 1):
            slot = start + timedelta(hours=offset)
            if slot >= NOW:
                break
            total += per_hour
            rows.append({"start": slot, "state": total, "sum": total})
        async_import_statistics(hass, _meta(entity), rows)
    await async_wait_recording_done(hass)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="probe",
        version=3,
        data={CONF_PROFILE: json.loads(profile.to_json()), CONF_FORECAST_HOURS: 4},
        options={
            CONF_GRID_IMPORT_ENTITY: IMPORT_ENTITY,
            CONF_GRID_EXPORT_ENTITY: EXPORT_ENTITY,
            CONF_CYCLE_START_DAY: 1,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    response = await hass.services.async_call(
        DOMAIN,
        "backfill_usage",
        {"config_entry": entry.entry_id, "start": "2026-07-01"},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    # Whole finished days only: today is the running totals' job.
    assert response["first_day"] == "2026-07-01"
    assert response["last_day"] == "2026-07-20"
    assert response["days"] == 20
    assert response["grid_import_kwh"] == pytest.approx(24.0 * 20)
    assert response["grid_export_kwh"] == pytest.approx(6.0 * 20)
    assert response["net_cost"] > 0
    assert response["skipped"] == []

    await async_wait_recording_done(hass)
    stat_id = "tariffkit:probe_net_cost"
    assert stat_id in response["statistic_ids"]
    written = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 7, 1, tzinfo=PACIFIC),
        None,
        {stat_id},
        "day",
        None,
        {"sum", "state"},
    )
    assert len(written[stat_id]) == 20

    # Rerunning replaces rather than appends -- the whole point of writing
    # external statistics instead of accumulating into the entities' series.
    again = await hass.services.async_call(
        DOMAIN,
        "backfill_usage",
        {"config_entry": entry.entry_id, "start": "2026-07-01"},
        blocking=True,
        return_response=True,
    )
    await async_wait_recording_done(hass)
    assert again == response
    rewritten = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 7, 1, tzinfo=PACIFIC),
        None,
        {stat_id},
        "day",
        None,
        {"sum", "state"},
    )
    assert len(rewritten[stat_id]) == 20, "a rerun must not duplicate days"


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_the_action_refuses_without_meters(hass: HomeAssistant) -> None:
    import json

    from custom_components.tariffkit.const import CONF_PROFILE, DOMAIN
    from homeassistant.exceptions import ServiceValidationError
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="probe",
        version=3,
        data={CONF_PROFILE: json.loads(_profile().to_json())},
        options={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError, match="no meter entities"):
        await hass.services.async_call(
            DOMAIN,
            "backfill_usage",
            {"config_entry": entry.entry_id},
            blocking=True,
            return_response=True,
        )
