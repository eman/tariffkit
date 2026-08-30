"""Pricing metered history into long-term statistics."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from custom_components.tariffkit import backfill
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from tariffkit import Config
from tariffkit.account import AccountEpoch, AccountProfile
from tariffkit.billing import BillingPeriod, IntervalReading, apply_credits
from tariffkit.timeutil import PACIFIC


def _profile(
    tariff: str = "E-ELEC", pto_date: date = date(2026, 1, 1), **kwargs: object
) -> AccountProfile:
    config = Config(tariff=tariff, pto_date=pto_date, **kwargs)  # type: ignore[arg-type]
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

    walk = backfill.walk_cycle(profile, readings, cycle)
    days = backfill.decompose(walk, None)
    cycle_bill, reason = walk.bill, walk.reason
    assert reason == ""
    assert len(days) == 20

    whole, _ = price(profile, readings, cycle)
    assert whole is not None
    # The cycle bill the caller gets back is the same bill, not a re-derivation.
    assert cycle_bill is not None
    assert cycle_bill.total == pytest.approx(whole.total, abs=1e-9)
    assert sum(d.amount_due for d in days) == pytest.approx(apply_credits(whole).cash_due, abs=1e-9)
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

    walk = backfill.walk_cycle(profile, readings, cycle)
    days = backfill.decompose(walk, None)
    cycle_bill, reason = walk.bill, walk.reason
    assert reason == ""
    whole, _ = price(profile, readings, cycle)
    assert whole is not None
    assert cycle_bill is not None
    assert cycle_bill.total == pytest.approx(whole.total, abs=1e-9)
    assert sum(d.amount_due for d in days) == pytest.approx(apply_credits(whole).cash_due, abs=1e-9)


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
    days = backfill.decompose(backfill.walk_cycle(profile, readings, cycle), None)
    whole, _ = price(profile, readings, cycle)
    assert whole is not None
    assert whole.total < 0, "a month of net export earns more than it owes"
    # But a statement never prints a negative: the surplus banks instead. The
    # published days decompose what is owed, so they sum to that rather than to
    # `Bill.total`, and they still bracket a heavy day above the cycle.
    owed = apply_credits(whole).cash_due
    # Not `>= 0`, which `apply_credits` guarantees by construction and so
    # asserts nothing. The point is that the credit was capped at the charges it
    # could reach rather than handed back: the cycle's own total is deeply
    # negative and what it charges is not.
    assert whole.total < 0 < owed
    assert max(d.amount_due for d in days) > owed
    assert sum(d.amount_due for d in days) == pytest.approx(owed, abs=1e-9)


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
    assert response["amount_due"] > 0
    assert response["skipped"] == []

    await async_wait_recording_done(hass)
    stat_id = "tariffkit:probe_amount_due"
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
    assert any("gap(s) in the series" in w for w in result.warnings)
    assert result.summary("probe")["complete"] is False


def test_a_clean_window_reports_itself_complete() -> None:
    """Clean means the bank too: the window opens where compensation did."""
    profile = _profile(pto_date=date(2026, 7, 1))
    readings = _hours(date(2026, 7, 1), date(2026, 7, 20), imported=1.0)
    result = backfill.build(profile, readings, date(2026, 7, 1), date(2026, 7, 20), 1)
    assert result.warnings == []
    assert result.skipped == []
    assert result.summary("probe")["complete"] is True


def test_a_window_starting_after_pto_says_its_bank_opens_at_zero() -> None:
    """Otherwise every amount due in it is overstated, silently.

    A backfill opens the bank at zero, which is true only when it starts at the
    cycle holding Permission To Operate. Started later, the credit earned in
    between is missing and would have offset these very charges -- and nothing
    else in the result says so: no cycle is skipped and no day is unpriced.
    """
    profile = _profile(pto_date=date(2026, 1, 1))
    readings = _hours(date(2026, 7, 1), date(2026, 7, 20), imported=1.0)
    result = backfill.build(profile, readings, date(2026, 7, 1), date(2026, 7, 20), 1)
    assert result.skipped == []
    assert any("opens the export credit bank at zero" in w for w in result.warnings)
    assert any("Backfill from 2026-01-01" in w for w in result.warnings)
    assert result.summary("probe")["complete"] is False


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
    assert all(d.amount_due > 0 for d in result.days)
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


def test_the_reader_marks_only_the_hour_that_receives_a_catch_up() -> None:
    """`estimated` is the library's own word for a reconstructed interval.

    Only the hour *after* a hole. The hour before it has both its own recorded
    sum and its predecessor's, so its change is exact and its day is priceable;
    refusing that day as well would cost a correctly-metered day for every
    outage and move its cost into the residual for no reason.
    """
    from custom_components.tariffkit.energy import MeterSettings, UsageReader

    reader = UsageReader(None, MeterSettings(import_entity="sensor.x"))  # type: ignore[arg-type]
    hour = 3600.0
    covered = {"sensor.x": {hour * 1, hour * 2, hour * 5, hour * 6}}
    assert reader._reconstructed(covered) == {hour * 5}
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

    # And each cycle's published days sum to what that cycle actually owed --
    # the ledger's figure, with the bank carried in, not the bill's own total.
    for entry in ledger.entries:
        days = [d for d in result.days if entry.period.start <= d.day <= entry.period.end]
        assert sum(d.amount_due for d in days) == pytest.approx(entry.cash_due, abs=1e-9)
    assert result.residual == pytest.approx(0.0, abs=1e-9)


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


def test_a_refused_day_is_reported_as_a_residual_not_left_to_be_discovered() -> None:
    """The daily rows and the cycle bill genuinely disagree, so say by how much.

    A day with no readings contributes nothing to its cycle either, so omitting
    it keeps the sum. A day holding a reconstructed hour does not: that energy
    is real and stays in the cycle's own bill, which depends only on the
    counter's endpoints. Two figures in one answer must not describe the same
    period and quietly differ.
    """
    profile = _profile()
    readings = _hours(date(2026, 7, 1), date(2026, 7, 31), imported=0.5, exported=0.2)
    catch_up = next(
        i
        for i, r in enumerate(readings)
        if r.start.astimezone(PACIFIC).date() == date(2026, 7, 15)
        and r.start.astimezone(PACIFIC).hour == 5
    )
    readings[catch_up] = IntervalReading(
        readings[catch_up].start, imported=20.0, exported=0.2, estimated=True
    )
    result = backfill.build(profile, readings, date(2026, 7, 1), date(2026, 7, 31), 1)

    assert result.unpriced, "the reconstructed day is not published"
    assert len(result.bills) == 1
    published = sum(day.amount_due for day in result.days)
    priced = result.lifetime.cash_due
    assert priced > published, "the cycle keeps energy the days do not"
    assert result.residual == pytest.approx(priced - published)

    summary = result.summary("probe")
    assert summary["days_unpriced"] == len(result.unpriced)
    assert summary["residual"] == pytest.approx(round(priced - published, 2))
    # The two figures in the payload differ by exactly the residual, stated.
    assert summary["cycles"][0]["total"] - summary["amount_due"] == pytest.approx(
        summary["residual"], abs=0.01
    )


def test_a_clean_cycle_has_no_residual() -> None:
    profile = _profile()
    readings = _hours(date(2026, 7, 1), date(2026, 7, 31), imported=0.5, exported=0.2)
    result = backfill.build(profile, readings, date(2026, 7, 1), date(2026, 7, 31), 1)
    assert result.unpriced == []
    assert result.residual == pytest.approx(0.0, abs=1e-9)
    assert result.summary("probe")["residual"] == 0.0


def test_a_rerun_that_publishes_fewer_days_leaves_no_orphan() -> None:
    """Writing external statistics inserts or updates; it never deletes.

    So a day a later run refuses to price would otherwise stay published at the
    price an earlier run gave it, with the following day absorbing the whole
    correction as a zero. Refusing a day has to mean it stops being published.
    """
    profile = _profile()
    clean = _hours(date(2026, 7, 1), date(2026, 7, 10), imported=1.0)
    first = backfill.build(profile, clean, date(2026, 7, 1), date(2026, 7, 10), 1)

    refused = list(clean)
    catch_up = next(
        i
        for i, r in enumerate(refused)
        if r.start.astimezone(PACIFIC).date() == date(2026, 7, 5)
        and r.start.astimezone(PACIFIC).hour == 0
    )
    refused[catch_up] = IntervalReading(refused[catch_up].start, imported=9.0, estimated=True)
    second = backfill.build(profile, refused, date(2026, 7, 1), date(2026, 7, 10), 1)
    assert date(2026, 7, 5) in second.unpriced

    series = next(s for s in backfill.SERIES if s.slug == "amount_due")
    before = backfill.statistics_for(first, series)
    after = backfill.statistics_for(second, series)
    # Every day the first run wrote is written again, so none is orphaned.
    assert len(after) == len(before) == 10
    refused_row = next(row for row in after if row["start"].date() == date(2026, 7, 5))
    assert refused_row["state"] == 0.0, "a day nothing can be said about reads zero"
    # And the sum carries across it rather than jumping.
    sums = [row["sum"] for row in after]
    assert sums == sorted(sums), "the running total never goes backwards"


def test_the_published_history_settles_the_year_the_live_bank_does() -> None:
    """Otherwise the graph and the sensor state the same cycle differently.

    ``build`` used to chain ``apply_credits`` from cycle to cycle, which carries
    a bank but never settles a year. The live entities fold through
    ``bank.fold``, which applies every annual clawback. Past an anniversary the
    two diverged by hundreds of dollars, and the published one was the wrong
    one -- it spends credit a cash-out already paid out in cash.
    """
    from custom_components.tariffkit.bank import fold

    from tariffkit.billing import run_ledger

    profile = _profile(pto_date=date(2026, 1, 1))
    readings = _hours(date(2026, 1, 1), date(2027, 8, 31), imported=0.2, exported=3.0)
    result = backfill.build(profile, readings, date(2026, 1, 1), date(2027, 8, 31), 1)

    assert result.skipped == []
    assert result.lifetime.events, "twenty months crosses at least one annual settlement"
    # The straight fold the old code did, for contrast.
    naive = run_ledger(result.bills)
    assert result.lifetime.closing.total < naive.closing.total

    # And what the live entities would show for the same run.
    assert fold(profile, list(result.bills)).balance.total == pytest.approx(
        result.lifetime.closing.total
    )
    assert result.residual == pytest.approx(0.0, abs=1e-9)


def test_a_settlement_lowers_what_the_cycles_after_it_can_offset() -> None:
    """Which is the whole reason the published amounts changed.

    A cycle opening after a cash-out has less bank to spend, so it owes more.
    """
    profile = _profile(pto_date=date(2026, 1, 1))
    readings = _hours(date(2026, 1, 1), date(2027, 8, 31), imported=0.2, exported=3.0)
    result = backfill.build(profile, readings, date(2026, 1, 1), date(2027, 8, 31), 1)

    settled = result.lifetime.events[0].period.end
    before = [e for e in result.lifetime.entries if e.period.end <= settled]
    after = [e for e in result.lifetime.entries if e.period.start > settled]
    assert before and after
    assert after[0].opening.total < before[-1].closing.total, "the clawback reached the next cycle"


def test_a_recorded_hour_this_refuses_is_not_reported_as_missing() -> None:
    """An hour the recorder wrote is not an hour the recorder lost.

    The coverage warning counted every hour absent from `covered`, and an hour
    whose counter jumped further than a house can draw is refused rather than
    absent. Reporting it as missing told an owner their meter history had holes
    in it when every hour was on disk.
    """
    import asyncio
    from datetime import datetime, timedelta

    from custom_components.tariffkit.energy import MeterSettings, UsageReader

    from tariffkit.timeutil import PACIFIC

    def warnings_for(*, refuse: bool) -> tuple[str, ...]:
        reader = UsageReader(None, MeterSettings(import_entity="sensor.x"))  # type: ignore[arg-type]
        opens = datetime(2026, 6, 1, tzinfo=PACIFIC)
        rows: list[dict[str, float]] = []
        total = 0.0
        for index in range(73):
            hole = bool(index) and index % 6 == 0
            if hole and not refuse:
                continue  # the recorder never wrote this hour
            total += 500.0 if hole else 0.5
            rows.append(
                {"start": (opens - timedelta(hours=1)).timestamp() + index * 3600, "sum": total}
                | {"change": 500.0 if hole else 0.5, "state": total}
            )

        async def query(window: datetime, until: datetime) -> dict[str, list[dict[str, float]]]:
            del window, until
            return {"sensor.x": rows}

        reader._async_query = query  # type: ignore[method-assign]
        asyncio.run(reader.async_readings(date(2026, 6, 1), date(2026, 6, 3)))
        return reader.absent

    refused = warnings_for(refuse=True)
    assert len(refused) == 1
    assert "could not use" in refused[0]
    assert "is missing" not in refused[0], "every hour was on disk"

    # The genuine hole still reads as one, in the same words as before.
    absent = warnings_for(refuse=False)
    assert absent == ("sensor.x is missing 12 of 72 hour(s) in this window",)


def test_the_summary_cycles_carry_the_terms_that_reconcile_them() -> None:
    """The payload has to add up for the same reason the entity attributes do.

    `cash_due` is `max(0, gross_charges - credit_applied)`, so a consumer given
    only the charge components lands short of it by whatever the statement spent
    in-cycle instead of banking. Both terms are published; this is what notices
    if either stops being.
    """
    profile = _profile()
    readings = _hours(date(2026, 6, 1), date(2026, 7, 31), imported=0.2, exported=2.0)
    result = backfill.build(profile, readings, date(2026, 6, 1), date(2026, 7, 31), 1)
    from tariffkit.billing import run_ledger

    entries = {entry.period: entry for entry in run_ledger(result.bills).entries}

    cycles = result.summary("probe")["cycles"]
    assert len(cycles) == 2
    for cycle in cycles:
        entry = entries[
            next(b.period for b in result.bills if b.period.start.isoformat() == cycle["start"])
        ]
        assert cycle["gross_charges"] == pytest.approx(round(entry.gross_charges, 2))
        assert cycle["in_cycle_offsets"] == pytest.approx(round(entry.in_cycle_offsets.total, 2))
        assert cycle["non_offsettable"] == pytest.approx(round(entry.non_offsettable, 2))
        # The charge components close on `gross_charges` through the offset,
        # the same identity the entity attributes carry.
        assert (
            cycle["energy_charges"]
            + cycle["taxes"]
            + cycle["fixed_charges"]
            - cycle["in_cycle_offsets"]
        ) == pytest.approx(cycle["gross_charges"], abs=0.02)
        # Each term is rounded to cents independently, so the identity over the
        # payload cannot hold tighter than the three roundings it carries.
        assert cycle["cash_due"] == pytest.approx(
            max(0.0, cycle["gross_charges"] - cycle["credit_applied"]), abs=0.02
        )


async def test_the_sum_is_anchored_at_the_first_row_written(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused leading day must not leave its old figure inside the base.

    `statistics_for` writes a zero row for every day in priced + unpriced, so a
    rerun whose first day flips to refused opens the span earlier than
    `days[0]`. Anchoring the base at `days[0]` left that day's previous
    contribution in the base and re-added it under a row reading 0.0, so the
    day still charged and every later day carried it -- permanently, since
    external statistics are never deleted.
    """
    from custom_components.tariffkit import backfill

    asked: list[date] = []

    async def recording(_hass: HomeAssistant, _profile: str, opens: date) -> dict[str, float]:
        asked.append(opens)
        return {}

    monkeypatch.setattr(backfill, "async_base_sums", recording)
    # Imported inside `async_publish`, so it has to be patched at its source.
    monkeypatch.setattr(
        "homeassistant.components.recorder.statistics.async_add_external_statistics",
        lambda *a, **k: None,
    )

    result = backfill.BackfillResult(
        days=[backfill.DayFigures(day=date(2026, 7, 2), grid_import=24.0)],
        unpriced=[date(2026, 7, 1)],
    )
    await backfill.async_publish(hass, "home", result)

    assert asked == [date(2026, 7, 1)], "base must be read from the earliest written row"
