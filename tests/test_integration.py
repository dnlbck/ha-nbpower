"""Home Assistant-level tests: setup, sensors, config flow, statistics backfill."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from conftest import (
    ACCOUNT_NUMBER,
    _daily_window,
    _interval_rows,
    _monthly_cycles,
    server_base,
)
from custom_components.nbpower.const import DOMAIN
from custom_components.nbpower.sensor import ENERGY_KEY, UNIQUE_ID_TEMPLATE

USERNAME = "user@example.com"
PASSWORD = "s3cret"
ACCOUNT_TITLE = "NB Power 1234567"
FLOW_INPUT = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


def _parse_day(text: str) -> date:
    return datetime.strptime(text, "%B %d, %Y").date()


def _expected_cumulative() -> float:
    """Monthly cycles + post-cycle daily rows + fresh 15-minute days.

    The interval feed publishes intraday; days strictly after the daily
    books' last day (today-4 in the mock) fold into the cumulative.
    """
    monthly = _monthly_cycles()
    daily = [r for r in _daily_window() if r["Consumption"] != ""]
    last_cycle_end = max(_parse_day(r["ToDate"]) for r in monthly)
    daily_end = max(_parse_day(r["UsageDate"]) for r in daily)
    post = [r for r in daily if _parse_day(r["UsageDate"]) > last_cycle_end]
    books = sum(r["Consumption"] for r in monthly) + sum(r["Consumption"] for r in post)
    return books


def _expected_history_points() -> list[tuple[date, float]]:
    """Days + kWh the statistics backfill should produce (cycles spread)."""
    monthly = _monthly_cycles()
    points: list[tuple[date, float]] = []
    for cycle in monthly:
        start = _parse_day(cycle["FromDate"])
        end = _parse_day(cycle["ToDate"])
        days = (end - start).days + 1
        per_day = round(cycle["Consumption"] / days, 4)
        points.extend((start + timedelta(days=i), per_day) for i in range(days))
    last_cycle_end = max(_parse_day(r["ToDate"]) for r in monthly)
    for row in _daily_window():
        if row["Consumption"] == "":
            continue
        day = _parse_day(row["UsageDate"])
        if day > last_cycle_end:
            points.append((day, row["Consumption"]))
    today = date.today()
    return [p for p in points if p[0] < today]


async def _setup_entry(
    hass: HomeAssistant, portal, nbpower_urls, *, backfill_hourly: bool = True
) -> MockConfigEntry:
    nbpower_urls(server_base(portal))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=ACCOUNT_TITLE,
        data=FLOW_INPUT,
        options=None if backfill_hourly else {"backfill_hourly": False},
        unique_id=f"account_{ACCOUNT_NUMBER}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")


async def test_setup_creates_sensors(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup_entry(hass, portal, nbpower_urls)

    for key in (
        "energy_usage",
        "cost_usage",
        "last_daily_energy",
        "month_to_date_energy",
        "month_to_date_cost",
        "projected_energy",
        "projected_bill",
    ):
        assert _entity_id(hass, entry, key)

    energy = hass.states.get(_entity_id(hass, entry, ENERGY_KEY))
    assert energy is not None and energy.state != "unknown"
    assert energy.attributes["unit_of_measurement"] == "kWh"
    assert energy.attributes["state_class"] == "total_increasing"
    assert energy.attributes["device_class"] == "energy"

    cost = hass.states.get(_entity_id(hass, entry, "cost_usage"))
    assert cost.attributes["device_class"] == "monetary"
    assert cost.attributes["state_class"] == "total"
    assert float(cost.state) > 0

    # The month-to-date numbers come from the monthly payload, not the
    # daily payload's zeroed placeholder block.
    mtd = hass.states.get(_entity_id(hass, entry, "month_to_date_energy"))
    assert float(mtd.state) == pytest.approx(1388.0)

    bill = hass.states.get(_entity_id(hass, entry, "projected_bill"))
    assert float(bill.state) == pytest.approx(228.13)


async def test_cumulative_sensors_never_decrease(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """A downward revision is held, not reported (no meter-reset spikes)."""
    entry = await _setup_entry(hass, portal, nbpower_urls, backfill_hourly=False)
    energy_id = _entity_id(hass, entry, ENERGY_KEY)
    before = float(hass.states.get(energy_id).state)

    portal.app["state"]["revise_down"] = True
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    after = float(hass.states.get(energy_id).state)
    assert after == before


async def test_energy_sensor_derives_from_cycles_plus_daily(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup_entry(hass, portal, nbpower_urls)
    energy = hass.states.get(_entity_id(hass, entry, ENERGY_KEY))
    assert float(energy.state) == pytest.approx(_expected_cumulative(), abs=1.0)
    # Today's 15-minute data is not published yet (the server returns the
    # latest published day, which the client filters out).
    assert energy.attributes["today_kwh"] == 0.0


async def test_history_statistics_imported(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Phase 1: day-resolution import (hourly upgrade disabled)."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    entry = await _setup_entry(hass, portal, nbpower_urls, backfill_hourly=False)
    await get_instance(hass).async_block_till_done()

    statistic_id = _entity_id(hass, entry, ENERGY_KEY)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        dt_util.now(),
        (statistic_id,),
        "hour",
        None,
        {"state", "sum"},
    )
    rows = stats[statistic_id]
    points = _expected_history_points()
    assert len(rows) == len(points)
    # The recorder convention: `sum` is cumulative; the Energy dashboard
    # charts change = consecutive sum differences.
    sums = [r["sum"] for r in rows]
    changes = [b - a for a, b in zip([0.0] + sums[:-1], sums)]
    expected = [kwh for _, kwh in sorted(points)]
    assert changes == pytest.approx(expected, abs=0.01)
    assert min(changes) >= 0
    assert sums[-1] == pytest.approx(sum(expected), abs=1.0)
    assert rows[0]["state"] == pytest.approx(0.0)


async def test_config_flow_success(
    hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations
):
    nbpower_urls(server_base(portal))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        FLOW_INPUT,
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == ACCOUNT_TITLE
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_config_flow_wrong_password(
    hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations
):
    nbpower_urls(server_base(portal))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_with_credentials_recovers_entry(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Bad credentials trigger re-auth; correct ones recover the entry."""
    nbpower_urls(server_base(portal))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=ACCOUNT_TITLE,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"},
        unique_id=f"account_{ACCOUNT_NUMBER}",
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is entry.state.SETUP_ERROR

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], FLOW_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == PASSWORD
    # The reload after re-auth brings the entry up.
    assert entry.state is entry.state.LOADED
    assert hass.states.get(_entity_id(hass, entry, ENERGY_KEY))


async def test_old_token_entry_prompts_reauth(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """A pre-0.4 token-based entry fails auth and asks for credentials."""
    nbpower_urls(server_base(portal))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=ACCOUNT_TITLE,
        data={"token": "old-token==", "account_number": "1234567"},
        unique_id=f"account_{ACCOUNT_NUMBER}",
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    )


async def test_token_expiry_relogin_recovers(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """A mid-refresh token expiry re-logins transparently; data keeps flowing."""
    entry = await _setup_entry(hass, portal, nbpower_urls, backfill_hourly=False)
    energy_id = _entity_id(hass, entry, ENERGY_KEY)
    before = float(hass.states.get(energy_id).state)

    # Kill the widget token: the next refresh must fail, re-login, retry.
    # (The mock mints a fresh token on successful login.)
    portal.app["state"]["expire_widget_token"] = True
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert entry.state is entry.state.LOADED
    assert float(hass.states.get(energy_id).state) >= before


async def test_duplicate_account_rejected(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    await _setup_entry(hass, portal, nbpower_urls)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        FLOW_INPUT,
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_hourly_backfill_upgrades_resolution(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
    monkeypatch,
):
    """The background task imports hourly rows from 15-minute data."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    entry = await _setup_entry(hass, portal, nbpower_urls)
    await get_instance(hass).async_block_till_done()
    await hass.async_block_till_done(wait_background_tasks=True)
    await get_instance(hass).async_block_till_done()

    statistic_id = _entity_id(hass, entry, ENERGY_KEY)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        dt_util.now(),
        (statistic_id,),
        "hour",
        None,
        {"state", "sum"},
    )
    rows = stats[statistic_id]
    # Hour-resolution upgrade: rows exist starting at non-midnight hours.
    by_hour_start = [
        r
        for r in rows
        if dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).hour != 0
    ]
    assert by_hour_start, "expected hourly (non-midnight) statistic rows"
    # Dashboard math: change = consecutive sum differences, all >= 0.
    ordered = sorted(rows, key=lambda r: r["start"])
    sums = [r["sum"] for r in ordered]
    changes = [b - a for a, b in zip([0.0] + sums[:-1], sums)]
    assert min(changes) >= 0, "negative change would render as a negative bar"
    # The state series must be monotonic — a series that resets to zero at
    # every midnight renders as negative flow in the Energy dashboard.
    states_series = [r["state"] for r in ordered if r["state"] is not None]
    assert states_series == sorted(states_series), "state series decreased"
    # Progress was persisted (backfill ran through the last published day).
    coordinator = entry.runtime_data
    assert coordinator.stored_interval_through() == date.today() - timedelta(days=1)


async def test_repair_generation_reimports_consistently(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Old-format storage triggers a full re-import with one state chain.

    This is the live failure path: pre-repair storage had stats_imported
    already true, so the hourly re-run found no history rows and seeded
    its state chain from zero instead of continuing the day-resolution
    series.
    """
    import json
    import os

    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period
    from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

    nbpower_urls(server_base(portal))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=ACCOUNT_TITLE,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        unique_id=f"account_{ACCOUNT_NUMBER}",
    )
    entry.add_to_hass(hass)

    # Simulate the pre-repair storage record.
    storage_path = hass.config.path("storage", f"nbpower.{entry.entry_id}")
    os.makedirs(os.path.dirname(storage_path), exist_ok=True)
    with open(storage_path, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "data": {"stats_imported": True}}, fh)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await get_instance(hass).async_block_till_done()

    statistic_id = _entity_id(hass, entry, ENERGY_KEY)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        dt_util.now(),
        (statistic_id,),
        "hour",
        None,
        {"state", "sum"},
    )
    rows = sorted(stats[statistic_id], key=lambda r: r["start"])
    states_series = [r["state"] for r in rows if r["state"] is not None]
    assert states_series == sorted(states_series), "state series decreased"

    # Hourly rows continue the day-resolution chain: the earliest hourly
    # state must be a large value (the pre-window cumulative), not ~0.
    hourly_rows = [
        r
        for r in rows
        if dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).hour != 0
    ]
    assert hourly_rows
    assert min(r["state"] for r in hourly_rows) > 1000


async def test_extension_imports_newly_published_days(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """After a refresh, newly published days are imported with real hours.

    Simulates the live failure: the backfill stopped at Sep 25 because
    Sep 26 was unpublished; the next refresh must pick it up.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    entry = await _setup_entry(hass, portal, nbpower_urls, backfill_hourly=False)
    coordinator = entry.runtime_data
    yesterday = date.today() - timedelta(days=1)

    # Simulate the backfill having stopped one day early.
    await coordinator.store_interval_through(yesterday - timedelta(days=1))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await get_instance(hass).async_block_till_done()

    statistic_id = _entity_id(hass, entry, ENERGY_KEY)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        dt_util.now(),
        (statistic_id,),
        "hour",
        None,
        {"state", "sum"},
    )
    rows = sorted(stats[statistic_id], key=lambda r: r["start"])
    sums = [r["sum"] for r in rows]
    changes = [b - a for a, b in zip([0.0] + sums[:-1], sums)]
    assert min(changes) >= 0
    # Yesterday now has 24 hourly rows with real usage.
    atl_rows = [
        r
        for r in rows
        if dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).date() == yesterday
    ]
    assert len(atl_rows) == 24
    assert coordinator.stored_interval_through() >= yesterday


async def test_partial_day_not_marked_complete(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Days publish partially; a partial day re-fetches without doubling."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    from conftest import _interval_rows

    portal.app["state"]["partial_day"] = True
    entry = await _setup_entry(hass, portal, nbpower_urls)
    coordinator = entry.runtime_data
    today = date.today()
    partial_day = today - timedelta(days=2)
    assert coordinator.stored_interval_through() == partial_day - timedelta(days=1)
    await get_instance(hass).async_block_till_done()

    statistic_id = _entity_id(hass, entry, ENERGY_KEY)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utc_from_timestamp(0), dt_util.now(),
        (statistic_id,), "hour", None, {"state", "sum"},
    )
    rows = sorted(stats[statistic_id], key=lambda r: r["start"])
    sums = [r["sum"] for r in rows]
    changes = [b - a for a, b in zip([0.0] + sums[:-1], sums)]
    assert min(changes) >= 0  # tail flattened despite the partial day

    partial_rows = [
        r for r in rows
        if dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).date() == partial_day
    ]
    assert len(partial_rows) == 16

    # Portal finishes publishing the day; next refresh completes it.
    portal.app["state"]["partial_day"] = False
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await get_instance(hass).async_block_till_done()

    assert coordinator.stored_interval_through() >= partial_day
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utc_from_timestamp(0), dt_util.now(),
        (statistic_id,), "hour", None, {"state", "sum"},
    )
    rows = sorted(stats[statistic_id], key=lambda r: r["start"])
    sums = [r["sum"] for r in rows]
    changes = [b - a for a, b in zip([0.0] + sums[:-1], sums)]
    assert min(changes) >= 0
    full_rows = [
        r for r in rows
        if dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).date() == partial_day
    ]
    assert len(full_rows) == 24
    day_change = sum(
        c for r, c in zip(rows, changes)
        if dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).date() == partial_day
    )
    expected = sum(r["Consumption"] for r in _interval_rows(partial_day))
    assert day_change == pytest.approx(expected, abs=0.05)  # counted exactly once


async def test_cost_history_imported(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Cost statistics get the same day-resolution history as energy."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    entry = await _setup_entry(hass, portal, nbpower_urls, backfill_hourly=False)
    await get_instance(hass).async_block_till_done()

    cost_id = _entity_id(hass, entry, "cost_usage")
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utc_from_timestamp(0), dt_util.now(),
        (cost_id,), "hour", None, {"state", "sum"},
    )
    rows = sorted(stats[cost_id], key=lambda r: r["start"])
    sums = [r["sum"] for r in rows]
    changes = [b - a for a, b in zip([0.0] + sums[:-1], sums)]
    assert len(rows) > 700  # three years of daily cost rows
    assert min(changes) >= 0
    # The cost chain ends at the sensor's own value (same books inputs).
    sensor_cost = float(hass.states.get(cost_id).state)
    assert sums[-1] == pytest.approx(sensor_cost, abs=1.0)


async def test_stats_frontier_pins_and_limits_imports(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """The import never writes hours the recorder owns.

    After setup the frontier is pinned; a later refresh with newly
    published days beyond the frontier does not import them.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period
    from datetime import datetime

    from conftest import _interval_rows

    entry = await _setup_entry(hass, portal, nbpower_urls)
    coordinator = entry.runtime_data
    await get_instance(hass).async_block_till_done()

    frontier = coordinator.stored_stats_frontier()
    assert frontier is not None and frontier <= datetime.now(frontier.tzinfo)

    statistic_id = _entity_id(hass, entry, ENERGY_KEY)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utc_from_timestamp(0), dt_util.now(),
        (statistic_id,), "hour", None, {"state", "sum"},
    )
    rows_before = sorted(stats[statistic_id], key=lambda r: r["start"])

    # Simulate the portal publishing more pre-frontier data: rewind the
    # partial-day marker so the extension re-fetches the frontier day.
    yesterday = date.today() - timedelta(days=1)
    await coordinator.store_interval_through(yesterday - timedelta(days=1))
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await get_instance(hass).async_block_till_done()

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utc_from_timestamp(0), dt_util.now(),
        (statistic_id,), "hour", None, {"state", "sum"},
    )
    rows_after = sorted(stats[statistic_id], key=lambda r: r["start"])
    sums = [r["sum"] for r in rows_after]
    prev = [0.0] + sums[:-1]
    changes = [b - a for a, b in zip(prev, sums)]
    assert min(changes) >= 0
    # No rows were added at or after the frontier.
    frontier_ts = frontier.timestamp()
    before_f = [r for r in rows_before if r["start"] < frontier_ts]
    after_f = [r for r in rows_after if r["start"] >= frontier_ts]
    assert before_f, "pre-frontier history present"
    assert len(after_f) <= len([r for r in rows_before if r["start"] >= frontier_ts])


async def test_frontier_day_prefrontier_hours_imported_and_anchor_syncs(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """A mid-day frontier splits its day; both halves end up counted once.

    The pre-frontier hours are import-owned (nothing else will ever write
    them after a restart wipes the recorder's short-term coverage), the
    post-frontier hours drive the sensor from the chain-end anchor, and
    the anchor syncs forward when the chain grows under the frontier.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    from conftest import _interval_rows

    entry = await _setup_entry(hass, portal, nbpower_urls, backfill_hourly=False)
    coordinator = entry.runtime_data
    await get_instance(hass).async_block_till_done()

    # A chain through the day before yesterday; frontier pinned at local
    # noon YESTERDAY (mid-day, like a restart-time pin). Seed far above
    # the mock books so the monotonic floor never engages.
    frontier_day = date.today() - timedelta(days=1)
    frontier = dt_util.start_of_local_day(frontier_day).replace(hour=12)
    seed = 80000.0
    await coordinator.store_interval_state_seed(seed)
    await coordinator.store_interval_through(frontier_day - timedelta(days=1))
    await coordinator.store_stats_frontier(frontier, seed)

    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await get_instance(hass).async_block_till_done()
    # Second refresh: the first one synced the anchor mid-flight.
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await get_instance(hass).async_block_till_done()

    rows_by_day = await _stats_rows(hass, _entity_id(hass, entry, ENERGY_KEY))
    day_rows = rows_by_day.get(frontier_day, [])
    hours = [dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).hour for r in day_rows]
    assert hours == list(range(12))  # pre-frontier hours only

    quarters = _interval_rows(frontier_day)
    pre = sum(r["Consumption"] for r in quarters if int(r["Hourly"][:2]) < 12)
    post = sum(r["Consumption"] for r in quarters if int(r["Hourly"][:2]) >= 12)

    # The day completed under the frontier: through advanced and the seed
    # and the sensor's anchor (frontier_end) both track the chain end —
    # not the stale pin-time value.
    assert coordinator.stored_interval_through() == frontier_day
    assert coordinator.stored_interval_state_seed() == pytest.approx(seed + pre, abs=0.05)
    assert coordinator.stored_frontier_end() == pytest.approx(seed + pre, abs=0.05)

    # Sensor = anchor + post-frontier intervals, counted exactly once
    # (the v0.10.1 live bug double-added them).
    sensor = float(hass.states.get(_entity_id(hass, entry, ENERGY_KEY)).state)
    assert sensor == pytest.approx(seed + pre + post, abs=0.15)


async def _stats_rows(hass, statistic_id: str) -> dict[date, list[dict]]:
    """All statistics rows for an entity, grouped by local day."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utc_from_timestamp(0), dt_util.now(),
        (statistic_id,), "hour", None, {"state", "sum"},
    )
    grouped: dict[date, list[dict]] = {}
    for row in sorted(stats[statistic_id], key=lambda r: r["start"]):
        day = dt_util.as_local(dt_util.utc_from_timestamp(row["start"])).date()
        grouped.setdefault(day, []).append(row)
    return grouped


async def test_align_lifts_reseeded_native_block_exactly_once(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """A recorder block that re-seeded below the chain is lifted once.

    ``adjust_statistics`` shifts the target row AND every later row (both
    tables). The walker must fold that into its cached sums or each
    consecutive repair compounds off stale values and balloons the tail —
    the phantom double +69k hours seen live on 2026-09-29.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
        StatisticMeanType,
    )
    from homeassistant.components.recorder.statistics import async_import_statistics

    from custom_components.nbpower.statistics import _async_align_native_block

    entry = await _setup_entry(hass, portal, nbpower_urls, backfill_hourly=False)
    await get_instance(hass).async_block_till_done()
    statistic_id = _entity_id(hass, entry, ENERGY_KEY)

    meta = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=statistic_id,
        unit_class="energy",
        unit_of_measurement="kWh",
    )
    midnight = dt_util.start_of_local_day(date.today())
    async_import_statistics(
        hass,
        meta,
        [
            StatisticData(start=midnight, state=100.0, sum=100.0),      # chain end
            StatisticData(start=midnight + timedelta(hours=1), state=0.0, sum=40.0),   # re-seeded
            StatisticData(start=midnight + timedelta(hours=2), state=40.0, sum=42.0),
            StatisticData(start=midnight + timedelta(hours=3), state=42.0, sum=41.0),  # interior dip
        ],
    )
    await get_instance(hass).async_block_till_done()

    await _async_align_native_block(hass, statistic_id, midnight, 100.0)
    await get_instance(hass).async_block_till_done()

    rows = (await _stats_rows(hass, statistic_id))[date.today()]
    sums = {dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).hour: r["sum"] for r in rows}
    # Lifted onto the chain end once; interior delta (42-40) preserved and
    # the dip repaired to the running level — no compounding.
    assert sums[0] == pytest.approx(100.0, abs=0.01)
    assert sums[1] == pytest.approx(100.0, abs=0.01)
    assert sums[2] == pytest.approx(102.0, abs=0.01)
    assert sums[3] == pytest.approx(102.0, abs=0.01)
