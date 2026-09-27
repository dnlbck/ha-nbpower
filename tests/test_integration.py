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
    today = date.today()
    recent = 0.0
    day = today
    while day > daily_end:
        rows = _interval_rows(day)
        recent += sum(r["Consumption"] for r in (rows[:12] if day == today else rows))
        day -= timedelta(days=1)
    return books + round(recent, 3)


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
    assert cost.attributes["state_class"] == "total_increasing"
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
    # Today's partial 15-minute data is exposed as an attribute.
    assert energy.attributes["today_kwh"] > 0


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
