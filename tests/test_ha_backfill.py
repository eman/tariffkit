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

    days, cycle_bill, reason, _ = backfill.price_cycle(profile, readings, cycle)
    assert reason == ""
    assert len(days) == 20

    whole, _ = price(profile, readings, cycle)
    assert whole is not None
    # The cycle bill the caller gets back is the same bill, not a re-derivation.
    assert cycle_bill is not None
    assert cycle_bill.total == pytest.approx(whole.total, abs=1e-9)
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

    days, cycle_bill, reason, _ = backfill.price_cycle(profile, readings, cycle)
    assert reason == ""
    whole, _ = price(profile, readings, cycle)
    assert whole is not None
    assert cycle_bill is not None
    assert cycle_bill.total == pytest.approx(whole.total, abs=1e-9)
    assert sum(d.net_cost for d in days) == pytest.approx(whole.total, abs=1e-9)


def test_a_single_day_may_exceed_its_cycle() -> None:
    """Summing to the cycle does not bound any individual day.

    A heavy import day inside a cycle that exports for the rest of the month
    costs more on its own than the cycle it belongs to, because the later days
    earn credit against it. Documented because the opposite reads as an obvious
    corollary of the sum property, and is not one.
    """
    from custom_components.tariffkit.energy import price

    profile = _profile()
    cycle = BillingPeriod(date(2026, 7, 1), date(2026, 7, 31))
    readings = [
        IntervalReading(datetime(2026, 7, 1, hour, tzinfo=PACIFIC), imported=8.0)
        for hour in range(24)
    ]
    for day in range(2, 32):
        readings += [
            IntervalReading(datetime(2026, 7, day, hour, tzinfo=PACIFIC), exported=8.0)
            for hour in range(24)
        ]
    days, _, _, _ = backfill.price_cycle(profile, readings, cycle)
    whole, _ = price(profile, readings, cycle)
    assert whole is not None
    assert whole.total < 0, "a month of net export owes nothing"
    assert max(d.net_cost for d in days) > whole.total
    assert sum(d.net_cost for d in days) == pytest.approx(whole.total, abs=1e-9)


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


def test_cycles_keep_their_true_start_and_only_their_tail_is_clipped() -> None:
    """A cycle's head must never be clipped to the window.

    `bill(cycle_start..D)` does not depend on days after D, so truncating a
    cycle's tail is harmless. Clipping its head is not: it discards whatever
    baseline allowance the real cycle had banked, and every day then prices too
    high. `build` refuses a leading cycle the window cannot cover rather than
    pricing it from the wrong start.
    """
    profile = _profile()
    cycles = backfill.cycles_between(profile, date(2026, 5, 15), date(2026, 7, 20), 1)
    assert [(c.start, c.end) for c in cycles] == [
        (date(2026, 5, 1), date(2026, 5, 31)),
        (date(2026, 6, 1), date(2026, 6, 30)),
        (date(2026, 7, 1), date(2026, 7, 20)),
    ]
    assert cycles[0].start == date(2026, 5, 1), "true start, not the window's edge"
    assert cycles[-1].end == date(2026, 7, 20), "the trailing cycle is truncated"


def test_a_window_opening_mid_cycle_refuses_that_cycle() -> None:
    """Rather than pricing its days against a cycle that never began there."""
    profile = _profile("E-TOU-C", baseline_territory="X")
    readings = _hours(date(2026, 5, 15), date(2026, 6, 30), imported=1.0)
    result = backfill.build(profile, readings, date(2026, 5, 15), date(2026, 6, 30), 1)

    assert any("starts inside this cycle" in s for s in result.skipped)
    assert all(d.day >= date(2026, 6, 1) for d in result.days)
    # June is whole, so it is priced in full.
    assert len(result.days) == 30


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


def test_a_hyphenated_profile_name_becomes_a_usable_statistic_id() -> None:
    """The config flow slugifies "My Home" to `my-home`, which HA rejects."""
    from homeassistant.components.recorder.statistics import VALID_STATISTIC_ID

    for name in ("home", "my-home", "My Home", "a__b", "-edge-"):
        for series in backfill.SERIES:
            statistic_id = series.statistic_id(name)
            assert VALID_STATISTIC_ID.match(statistic_id), statistic_id
    # Folding is lossy, so names that needed folding get a disambiguating
    # digest: three profiles Home Assistant keeps apart must not share a series.
    distinct = {backfill.statistic_slug(n) for n in ("my-home", "my_home", "my__home")}
    assert len(distinct) == 3, distinct
    # An already-valid name keeps its own spelling and pays nothing for this.
    assert backfill.statistic_slug("home") == "home"
    assert backfill.SERIES[0].statistic_id("home") == "tariffkit:home_grid_import"


def test_days_without_evidence_are_not_billed() -> None:
    """A day the recorder holds nothing for is not a day of zero usage.

    The default window starts at the profile's first epoch, routinely years
    before the meter sensor existed. Pricing those days charges a daily charge
    for days there is no evidence about, and the written row is then
    indistinguishable from a real zero-usage day.
    """
    profile = _profile()
    readings = _hours(date(2026, 7, 1), date(2026, 7, 20), imported=1.0)
    result = backfill.build(profile, readings, date(2026, 5, 1), date(2026, 7, 20), 1)

    assert min(d.day for d in result.days) == date(2026, 7, 1)
    assert any("no metered readings before" in w for w in result.warnings)
    # 61 days of invented Base Services Charge is what this prevents.
    assert all(d.day >= date(2026, 7, 1) for d in result.days)


def test_no_readings_at_all_is_refused_rather_than_priced() -> None:
    profile = _profile()
    result = backfill.build(profile, [], date(2026, 7, 1), date(2026, 7, 20), 1)
    assert result.days == []
    assert any("holds no readings" in s for s in result.skipped)


def test_a_gap_in_the_metered_series_is_reported() -> None:
    """The live path warns about gaps; history must not be blinder than it."""
    profile = _profile()
    readings = [
        r
        for r in _hours(date(2026, 7, 1), date(2026, 7, 20), imported=1.0)
        if r.start.astimezone(PACIFIC).day not in (8, 9, 10)
    ]
    result = backfill.build(profile, readings, date(2026, 7, 1), date(2026, 7, 20), 1)
    assert any("gap(s) in the metered series" in w for w in result.warnings)
    assert result.summary("probe")["complete"] is False


def test_a_clean_window_reports_itself_complete() -> None:
    profile = _profile()
    readings = _hours(date(2026, 7, 1), date(2026, 7, 20), imported=1.0)
    result = backfill.build(profile, readings, date(2026, 7, 1), date(2026, 7, 20), 1)
    assert result.warnings == []
    assert result.skipped == []
    assert result.summary("probe")["complete"] is True


def test_the_running_sum_continues_from_what_precedes_the_window() -> None:
    """A rewritten window must not restart `sum` inside a live series.

    External statistics replace only the rows they name. A `sum` restarted at
    zero partway through makes the recorder derive a large negative value for
    the first rewritten day and corrupts every aggregate after it.
    """
    profile = _profile()
    # Aligned to the cycle, because `build` refuses one it joins partway
    # through; the action snaps a mid-cycle request back for exactly that reason.
    readings = _hours(date(2026, 7, 1), date(2026, 7, 3), imported=1.0)
    result = backfill.build(profile, readings, date(2026, 7, 1), date(2026, 7, 3), 1)
    series = next(s for s in backfill.SERIES if s.slug == "grid_import")

    fresh = backfill.statistics_for(result, series)
    assert [r["sum"] for r in fresh] == [
        pytest.approx(24.0),
        pytest.approx(48.0),
        pytest.approx(72.0),
    ]

    continued = backfill.statistics_for(result, series, base=1000.0)
    assert [r["sum"] for r in continued] == [
        pytest.approx(1024.0),
        pytest.approx(1048.0),
        pytest.approx(1072.0),
    ]
    # The per-day figures are untouched by where the series happens to be.
    assert [r["state"] for r in fresh] == [r["state"] for r in continued]


def test_interior_days_without_readings_are_not_billed_either() -> None:
    """Clipping the window's edges is not enough; a gap inside it counts too.

    A recorder outage in the middle of an otherwise covered window leaves days
    with no evidence, and pricing them puts a daily fixed charge on days nothing
    is known about. Skipping them leaves every other day's figure untouched,
    because a skipped day's charges cancel between the two cycle-to-date bills
    that bracket it.
    """
    profile = _profile()
    readings = [
        r
        for r in _hours(date(2026, 7, 1), date(2026, 7, 20), imported=1.0)
        if r.start.astimezone(PACIFIC).day not in (8, 9, 10)
    ]
    result = backfill.build(profile, readings, date(2026, 7, 1), date(2026, 7, 20), 1)

    priced = {d.day for d in result.days}
    assert date(2026, 7, 8) not in priced
    assert date(2026, 7, 9) not in priced
    assert date(2026, 7, 10) not in priced
    assert len(result.days) == 17
    assert any("could not be priced" in w for w in result.warnings)
    # The surviving days are unaffected -- each is still its own marginal share.
    assert all(d.net_cost > 0 for d in result.days)
    assert result.summary("probe")["complete"] is False


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_a_later_rerun_continues_the_sum_it_finds(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The case the first idempotency test could not reach.

    Backfill a wide window, then backfill only a later cycle. The second run
    rewrites rows that sit *after* rows the first run left in place, so its sums
    must continue from them. Restarting at zero splices a reset into a live
    series and the recorder derives a large negative day at the boundary.
    """
    import json

    from custom_components.tariffkit.const import (
        CONF_CYCLE_START_DAY,
        CONF_FORECAST_HOURS,
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
    rows, total = [], 0.0
    start = datetime(2026, 5, 1, tzinfo=PACIFIC) - timedelta(hours=1)
    while start < NOW:
        total += 1.0
        rows.append({"start": start, "state": total, "sum": total})
        start += timedelta(hours=1)
    async_import_statistics(hass, _meta(IMPORT_ENTITY), rows)
    await async_wait_recording_done(hass)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="probe",
        version=3,
        data={CONF_PROFILE: json.loads(_profile().to_json()), CONF_FORECAST_HOURS: 4},
        options={CONF_GRID_IMPORT_ENTITY: IMPORT_ENTITY, CONF_CYCLE_START_DAY: 1},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    async def run(day: str) -> dict:
        response = await hass.services.async_call(
            DOMAIN,
            "backfill_usage",
            {"config_entry": entry.entry_id, "start": day},
            blocking=True,
            return_response=True,
        )
        await async_wait_recording_done(hass)
        assert response is not None
        return dict(response)

    wide = await run("2026-05-01")
    assert wide["first_day"] == "2026-05-01"
    # Only July: strictly later than rows the first run already wrote.
    await run("2026-07-01")

    written = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 5, 1, tzinfo=PACIFIC),
        None,
        {"tariffkit:probe_grid_import"},
        "day",
        None,
        {"sum", "change"},
    )
    series = written["tariffkit:probe_grid_import"]
    changes = [row["change"] for row in series if row["change"] is not None]
    assert min(changes) >= 0, "no day may go backwards where the sum was restarted"
    assert sum(changes) == pytest.approx(wide["grid_import_kwh"], abs=0.01)


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_a_meter_with_no_history_is_reported_not_read_as_zero(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """One meter must not vouch for the other.

    Import statistics with no export statistics produce a gapless-looking series
    of `exported=0`. Judged on the merged readings that is indistinguishable
    from a site that exported nothing, and every credit it earned disappears.
    """
    import json

    from custom_components.tariffkit.const import (
        CONF_CYCLE_START_DAY,
        CONF_FORECAST_HOURS,
        CONF_GRID_EXPORT_ENTITY,
        CONF_GRID_IMPORT_ENTITY,
        CONF_PROFILE,
        DOMAIN,
    )
    from homeassistant.components.recorder.statistics import async_import_statistics
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    freezer.move_to(NOW)
    rows, total = [], 0.0
    start = datetime(2026, 7, 1, tzinfo=PACIFIC) - timedelta(hours=1)
    while start < NOW:
        total += 1.0
        rows.append({"start": start, "state": total, "sum": total})
        start += timedelta(hours=1)
    async_import_statistics(hass, _meta(IMPORT_ENTITY), rows)
    await async_wait_recording_done(hass)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="probe",
        version=3,
        data={CONF_PROFILE: json.loads(_profile().to_json()), CONF_FORECAST_HOURS: 4},
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
    assert response["grid_export_kwh"] == 0.0
    assert any(EXPORT_ENTITY in w for w in response["warnings"]), response["warnings"]
    assert response["complete"] is False


def test_a_day_carrying_another_day_s_energy_is_not_priced() -> None:
    """A counter catching up after an outage lands it all in one hour.

    The kWh total survives -- a cumulative counter depends only on its
    endpoints -- but the shape does not, and the shape is what a time-of-use
    tariff prices. That hour cannot be separated from the day's own usage, so
    the day it lands on cannot be priced, only guessed at.
    """
    profile = _profile()
    readings = [
        r
        for r in _hours(date(2026, 7, 1), date(2026, 7, 20), imported=1.0)
        if r.start.astimezone(PACIFIC).day not in (8, 9, 10)
    ]
    # The recorder returns on the 11th and reports the outage as one hour.
    catch_up = next(
        i
        for i, r in enumerate(readings)
        if r.start.astimezone(PACIFIC).date() == date(2026, 7, 11)
        and r.start.astimezone(PACIFIC).hour == 0
    )
    readings[catch_up] = IntervalReading(readings[catch_up].start, imported=73.0, estimated=True)

    result = backfill.build(profile, readings, date(2026, 7, 1), date(2026, 7, 20), 1)
    priced = {d.day for d in result.days}

    assert date(2026, 7, 11) not in priced, "the catch-up day must not be published"
    for missing in (date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10)):
        assert missing not in priced
    assert len(result.days) == 16
    assert any("catch-up" in w for w in result.warnings)
    assert result.summary("probe")["complete"] is False


def test_the_reader_marks_an_hour_that_follows_a_gap() -> None:
    """`estimated` is the library's own word for a reconstructed interval."""
    from custom_components.tariffkit.energy import MeterSettings, UsageReader

    reader = UsageReader(None, MeterSettings(import_entity="sensor.x"))  # type: ignore[arg-type]
    hour = 3600.0
    covered = {"sensor.x": {hour * 1, hour * 2, hour * 5, hour * 6}}
    assert reader._reconstructed(covered) == {hour * 5}, "only the hour after the gap"
    assert reader._reconstructed({"sensor.x": {hour, hour * 2, hour * 3}}) == set()


def test_the_cycle_bills_fold_into_a_credit_ledger() -> None:
    """What the bills are actually for.

    A day decomposition cannot carry an export credit bank; `run_ledger` folds
    *cycle* bills, applying each cycle's credits against its charges and banking
    the rest. Returning the bills rather than discarding them is what makes that
    possible from a backfill.
    """
    from tariffkit.billing import run_ledger

    profile = _profile()
    readings = _hours(date(2026, 6, 1), date(2026, 7, 31), imported=0.2, exported=2.0)
    result = backfill.build(profile, readings, date(2026, 6, 1), date(2026, 7, 31), 1)

    assert len(result.bills) == 2, "June and July"
    ledger = run_ledger(result.bills)
    assert len(ledger.entries) == 2
    # Heavy export against light import: credit is earned, partly applied, and
    # the remainder banks into the next cycle rather than settling.
    first, second = ledger.entries
    assert first.earned.total > 0
    assert first.closing.total > 0
    assert second.opening.total == pytest.approx(first.closing.total)
    assert second.closing.total > first.closing.total

    # And each bill matches the days published for it.
    for bill in result.bills:
        days = [d for d in result.days if bill.period.start <= d.day <= bill.period.end]
        assert sum(d.net_cost for d in days) == pytest.approx(bill.total, abs=1e-9)


def test_a_bank_folded_from_the_pto_cycle_opens_at_zero() -> None:
    """Which is why that cycle is the default place to start.

    Net Billing compensation runs from Permission To Operate, so the cycle
    containing it is the first that can earn anything. A ledger folded from
    there needs no opening balance, because there is nothing earlier to carry.
    """
    from tariffkit.billing import run_ledger

    pto = date(2026, 6, 15)
    profile = AccountProfile(
        (AccountEpoch(date(2026, 1, 1), Config(tariff="E-ELEC", pto_date=pto)),),
        name="probe",
    )
    # A cycle straddling PTO, then a whole one after it.
    readings = _hours(date(2026, 6, 1), date(2026, 7, 31), imported=0.2, exported=2.0)
    result = backfill.build(profile, readings, date(2026, 6, 1), date(2026, 7, 31), 1)

    ledger = run_ledger(result.bills)
    assert ledger.entries[0].opening.total == 0.0
    # The pre-PTO days of that first cycle earn nothing, and the engine says so.
    assert any("before the Permission To Operate date" in w for w in result.warnings)
