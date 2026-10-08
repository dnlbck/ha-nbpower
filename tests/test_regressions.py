"""Regression tests: the energy ledger, import completeness, recovery paths."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import conftest as nb
from custom_components.nbpower import config_flow
from custom_components.nbpower import statistics as nb_stats
from custom_components.nbpower.api import NBPowerClient
from custom_components.nbpower.const import DOMAIN

FLOW_INPUT = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "s3cret"}


async def _setup(hass, portal, nbpower_urls, *, backfill_hourly=True):
    nbpower_urls(nb.server_base(portal))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="NB Power 1234567",
        data=FLOW_INPUT,
        options=None if backfill_hourly else {"backfill_hourly": False},
        unique_id=f"account_{nb.ACCOUNT_NUMBER}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await get_instance(hass).async_block_till_done()
    return entry


def _eid(hass, entry, key="energy_usage"):
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")


def _val(hass, entry):
    return float(hass.states.get(_eid(hass, entry)).state)


async def _rows(hass, statistic_id):
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utc_from_timestamp(0), dt_util.now(),
        (statistic_id,), "hour", {"energy": "kWh"}, {"state", "sum"},
    )
    return sorted(stats.get(statistic_id, []), key=lambda r: r["start"])


def _interval_sum(day, from_hour=0):
    return sum(r["Consumption"] for r in nb._interval_rows(day) if int(r["Hourly"][:2]) >= from_hour)


async def _refresh(hass, coordinator):
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await get_instance(hass).async_block_till_done()


def _mi_calls(portal, since):
    return [c for c in portal.app["usage_calls"][since:] if c["Mode"] == "MI"]


@pytest.mark.parametrize("age", [11, 30])
async def test_sensor_keeps_counting_after_frontier_ages(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations, age
):
    entry = await _setup(hass, portal, nbpower_urls, backfill_hourly=False)
    coord = entry.runtime_data
    today = date.today()
    fday = today - timedelta(days=age)
    anchor = 80000.0
    await coord.store_interval_through(fday)
    await coord.store_interval_state_seed(anchor)
    await coord.store_stats_frontier(dt_util.start_of_local_day(fday).replace(hour=12), anchor)

    expected = anchor + _interval_sum(fday, 12) + sum(
        _interval_sum(today - timedelta(days=d)) for d in range(1, age)
    )
    n = len(portal.app["usage_calls"])
    await _refresh(hass, coord)
    assert _val(hass, entry) == pytest.approx(expected, abs=0.2)
    assert len(_mi_calls(portal, n)) == age + 1  # one-time catch-up walk

    # Settled days are not re-fetched: steady state is one request (today).
    n = len(portal.app["usage_calls"])
    await _refresh(hass, coord)
    assert _val(hass, entry) == pytest.approx(expected, abs=0.2)
    assert len(_mi_calls(portal, n)) == 1


async def test_settled_anchor_survives_reload(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations
):
    entry = await _setup(hass, portal, nbpower_urls, backfill_hourly=False)
    coord = entry.runtime_data
    fday = date.today() - timedelta(days=4)
    await coord.store_interval_through(fday)
    await coord.store_stats_frontier(dt_util.start_of_local_day(fday).replace(hour=12), 80000.0)
    await _refresh(hass, coord)
    value = _val(hass, entry)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    n = len(portal.app["usage_calls"])
    await _refresh(hass, entry.runtime_data)
    assert _val(hass, entry) == pytest.approx(value, abs=0.01)
    assert len(_mi_calls(portal, n)) == 1


async def test_first_setup_ledger_switch_does_not_step(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations
):
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    assert coord.stored_stats_frontier() is not None
    before = _val(hass, entry)
    await _refresh(hass, coord)
    assert _val(hass, entry) == pytest.approx(before, abs=0.01)
    assert coord.stored_frontier_end() == pytest.approx(before, abs=0.01)


async def test_frontier_day_hole_is_imported_once_published(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations,
    monkeypatch,
):
    entry = await _setup(hass, portal, nbpower_urls, backfill_hourly=False)
    coord = entry.runtime_data
    yday = date.today() - timedelta(days=1)
    frontier = dt_util.start_of_local_day(yday).replace(hour=12)
    seed = 80000.0
    await coord.store_interval_state_seed(seed)
    await coord.store_interval_through(yday - timedelta(days=1))
    await coord.store_stats_frontier(frontier, seed)

    real = nb._interval_rows
    monkeypatch.setattr(
        nb, "_interval_rows",
        lambda day: [r for r in real(day) if not (day == yday and 8 <= int(r["Hourly"][:2]) <= 15)],
    )
    await _refresh(hass, coord)
    assert coord.stored_interval_through() == yday - timedelta(days=1)  # not complete yet

    monkeypatch.setattr(nb, "_interval_rows", real)
    await _refresh(hass, coord)
    rows = await _rows(hass, _eid(hass, entry))
    hours = sorted(
        dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).hour
        for r in rows
        if dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).date() == yday
        and r["start"] < frontier.timestamp()
    )
    assert hours == list(range(12))
    assert coord.stored_interval_through() == yday
    pre = sum(r["Consumption"] for r in real(yday) if int(r["Hourly"][:2]) < 12)
    assert coord.stored_interval_state_seed() == pytest.approx(seed + pre, abs=0.05)


async def test_alignment_ignores_display_unit(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations
):
    from homeassistant.components.recorder.models import (
        StatisticData, StatisticMeanType, StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import async_import_statistics

    from custom_components.nbpower.statistics import _async_align_native_block

    entry = await _setup(hass, portal, nbpower_urls, backfill_hourly=False)
    sid = _eid(hass, entry)
    meta = StatisticMetaData(
        mean_type=StatisticMeanType.NONE, has_sum=True, name=None, source="recorder",
        statistic_id=sid, unit_class="energy", unit_of_measurement="kWh",
    )
    midnight = dt_util.start_of_local_day(date.today())
    async_import_statistics(hass, meta, [
        StatisticData(start=midnight, state=100.0, sum=100.0),
        StatisticData(start=midnight + timedelta(hours=1), state=100.0, sum=100.5),
        StatisticData(start=midnight + timedelta(hours=2), state=100.5, sum=101.0),
    ])
    await get_instance(hass).async_block_till_done()
    er.async_get(hass).async_update_entity_options(sid, "sensor", {"unit_of_measurement": "MWh"})
    await hass.async_block_till_done()

    await _async_align_native_block(hass, sid, midnight, 100.0)
    await get_instance(hass).async_block_till_done()
    sums = [r["sum"] for r in await _rows(hass, sid) if r["start"] >= midnight.timestamp()]
    assert sums == pytest.approx([100.0, 100.5, 101.0], abs=0.001)


async def test_old_partial_day_does_not_stall_backfill(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations,
    monkeypatch,
):
    monkeypatch.setattr(nb_stats, "INTERVAL_BACKFILL_DAYS", 15)
    outage = date.today() - timedelta(days=13)
    real = nb._interval_rows
    monkeypatch.setattr(
        nb, "_interval_rows",
        lambda day: [r for r in real(day) if not (day == outage and r["Hourly"].startswith("02:"))],
    )
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    assert coord.stored_interval_through() == date.today() - timedelta(days=1)
    rows = await _rows(hass, _eid(hass, entry))
    sums = [r["sum"] for r in rows]
    assert min(b - a for a, b in zip(sums, sums[1:])) >= 0
    outage_rows = [
        r for r in rows
        if dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).date() == outage
    ]
    assert len(outage_rows) == 23


async def test_backfill_option_off_imports_recent_window_only(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations,
    monkeypatch,
):
    """Off means no year-long backfill — but the recent days still import
    hourly, so the chain reaches the frontier the energy sensor needs."""
    monkeypatch.setattr(nb_stats, "INTERVAL_BACKFILL_DAYS", 15)
    monkeypatch.setattr(nb_stats, "INTERVAL_RECENT_DAYS", 5)
    entry = await _setup(hass, portal, nbpower_urls, backfill_hourly=False)
    await _refresh(hass, entry.runtime_data)  # the per-refresh pass, too

    rows = await _rows(hass, _eid(hass, entry))
    hourly_days = sorted({
        dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).date()
        for r in rows
        if dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).hour
    })
    assert hourly_days[0] == date.today() - timedelta(days=5)
    assert entry.runtime_data.stored_stats_frontier() is not None


@pytest.fixture
async def faulty_portal(socket_enabled):
    """The mock portal with switchable faults (login outage, rejected usage,
    a replacement account summary page)."""
    app = nb.build_app()

    @web.middleware
    async def faults(request, handler):
        state = app["state"]
        if state.get("login_503") and request.method == "POST" and "weblogin" in request.path:
            return web.Response(status=503, text="Service Unavailable")
        if (page := state.get("account_page")) and request.path.endswith("AccountSummaryView.aspx"):
            return web.Response(text=page, content_type="text/html")
        if (page := state.get("graph_page")) and request.path.endswith("ViewConsumptionGraph.aspx"):
            return web.Response(text=page, content_type="text/html")
        if state.get("reject_usage") and request.path.endswith("GetUsageGeneration"):
            return web.json_response(
                {"result": {"Status": 0, "Message": "Token has been expired.", "Data": None}}
            )
        return await handler(request)

    app.middlewares.append(faults)
    server = TestServer(app)
    await server.start_server()
    yield server
    await server.close()


def _reauth_flows(hass):
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == "reauth"
    ]


async def test_login_server_error_does_not_demand_reauth(
    recorder_mock, hass, faulty_portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup(hass, faulty_portal, nbpower_urls, backfill_hourly=False)
    faulty_portal.app["state"]["expire_widget_token"] = True
    faulty_portal.app["state"]["login_503"] = True
    await _refresh(hass, entry.runtime_data)
    assert not entry.runtime_data.last_update_success
    assert not _reauth_flows(hass)

    faulty_portal.app["state"]["login_503"] = False  # maintenance over
    await _refresh(hass, entry.runtime_data)
    assert entry.runtime_data.last_update_success


async def test_rejected_fresh_session_does_not_demand_reauth(
    recorder_mock, hass, faulty_portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    """A token refused right after a successful login is not a credentials
    problem (e.g. a WAF or a portal hiccup)."""
    entry = await _setup(hass, faulty_portal, nbpower_urls, backfill_hourly=False)
    faulty_portal.app["state"]["reject_usage"] = True
    await _refresh(hass, entry.runtime_data)
    assert not entry.runtime_data.last_update_success
    assert not _reauth_flows(hass)


async def test_reauth_rejects_a_different_account(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations, monkeypatch,
):
    entry = await _setup(hass, portal, nbpower_urls, backfill_hourly=False)

    async def _other_account(hass, username, password):
        return "7654321"

    monkeypatch.setattr(config_flow, "_validate_login", _other_account)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "someone@else.com", CONF_PASSWORD: "x"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data == FLOW_INPUT


async def test_import_lock_is_per_entry(hass, monkeypatch):
    """One account's running import must not make another's skip."""
    entry_a = MockConfigEntry(domain=DOMAIN, unique_id="account_a")
    entry_b = MockConfigEntry(domain=DOMAIN, unique_id="account_b")
    calls = []

    async def _probe(hass, entry, coordinator):
        calls.append(entry.entry_id)

    monkeypatch.setattr(nb_stats, "_async_import_hourly", _probe)
    lock_a = nb_stats._import_locks.setdefault(entry_a.entry_id, asyncio.Lock())
    async with lock_a:
        await nb_stats.async_backfill_hourly_statistics(hass, entry_a, None)  # A busy
        await nb_stats.async_backfill_hourly_statistics(hass, entry_b, None)
    assert calls == [entry_b.entry_id]


async def test_entries_get_their_own_sessions(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    """Portal logins live in cookies; a shared jar would let two accounts
    pick up each other's widget token."""
    nbpower_urls(nb.server_base(portal))
    entries = [
        MockConfigEntry(domain=DOMAIN, title=f"NB Power {s}", data=FLOW_INPUT, unique_id=f"account_{s}")
        for s in ("a", "b")
    ]
    for entry in entries:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entries[0].entry_id)  # sets up both
    await hass.async_block_till_done(wait_background_tasks=True)
    clients = [entry.runtime_data.client for entry in entries]
    assert clients[0]._session is not clients[1]._session
    assert all(entry.runtime_data.stored_stats_frontier() for entry in entries)


async def test_interval_feed_error_keeps_sensors_available(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations, monkeypatch,
):
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    before = _val(hass, entry)

    async def _flaky(day):
        raise aiohttp.ClientConnectionError("connection reset")

    monkeypatch.setattr(coord.client, "get_interval_usage", _flaky)
    await _refresh(hass, coord)
    assert coord.last_update_success
    assert _val(hass, entry) == pytest.approx(before)


async def test_last_daily_energy_skips_blank_row(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations, monkeypatch,
):
    real = nb._daily_window

    def _blank_newest():
        rows = real()
        rows[-1] = {**rows[-1], "Consumption": ""}
        return rows

    monkeypatch.setattr(nb, "_daily_window", _blank_newest)
    entry = await _setup(hass, portal, nbpower_urls, backfill_hourly=False)
    newest = [r for r in _blank_newest() if r["Consumption"] != ""][-1]
    state = hass.states.get(_eid(hass, entry, "last_daily_energy"))
    assert float(state.state) == pytest.approx(newest["Consumption"])


async def _user_flow(hass, user_input=FLOW_INPUT):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_config_flow_missing_widget_token_is_a_portal_error(
    hass, faulty_portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations, caplog,
):
    """Issue #1: a sign-in that lands on pages without the usage graph
    was reported as a network failure, with nothing in the log."""
    nbpower_urls(nb.server_base(faulty_portal))
    tokenless = nb.DEFAULT_FORM.format(
        action="/Customer/SelectAccount.aspx", extra=""
    ).replace("<html>", "<html><head><title>Select an Account</title></head>")
    faulty_portal.app["state"]["account_page"] = tokenless
    faulty_portal.app["state"]["graph_page"] = tokenless
    result = await _user_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "portal_error"}
    assert "No widget token on the account pages" in caplog.text
    assert "'/Customer/AccountSummaryView.aspx'" in caplog.text
    assert "'Select an Account'" in caplog.text
    assert "'/Customer/ViewConsumptionGraph.aspx'" in caplog.text
    assert FLOW_INPUT[CONF_PASSWORD] not in caplog.text


async def test_config_flow_token_from_consumption_graph_page(
    hass, faulty_portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    """Issue #2: an account whose summary page renders no usage graph
    (no ucConsumptionGraph control) still gets a token from the dedicated
    consumption page."""
    nbpower_urls(nb.server_base(faulty_portal))
    faulty_portal.app["state"]["account_page"] = nb.DEFAULT_FORM.format(
        action="/Customer/AccountSummaryView.aspx", extra=""
    ).replace("<html>", "<html><head><title>Account Summary</title></head>")
    result = await _user_flow(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_config_flow_surfaces_the_portal_token_error(
    hass, faulty_portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations, caplog,
):
    """The pages carry an accountSEWTokenError field the server fills in
    when it could not mint a token; the log should say that, not just
    'no token found'."""
    nbpower_urls(nb.server_base(faulty_portal))
    page = nb.DEFAULT_FORM.format(
        action="/Customer/AccountSummaryView.aspx",
        extra=(
            '<input type="hidden" '
            'name="ctl00$contentPlaceHolder$accountSEWTokenError" '
            'value="Account is not enrolled"/>'
        ),
    )
    faulty_portal.app["state"]["account_page"] = page
    faulty_portal.app["state"]["graph_page"] = page
    result = await _user_flow(hass)
    assert result["errors"] == {"base": "portal_error"}
    assert "Account is not enrolled" in caplog.text


async def test_config_flow_finds_a_relocated_widget_token(
    hass, faulty_portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    """The token field's control path follows the page layout."""
    nbpower_urls(nb.server_base(faulty_portal))
    faulty_portal.app["state"]["account_page"] = nb.DEFAULT_FORM.format(
        action="/Customer/AccountSummaryView.aspx",
        extra=(
            '<input type="hidden" name="ctl00$contentPlaceHolder$ucUsageGraph$'
            f'accountSEWToken" value="{nb.WIDGET_TOKEN}"/>'
        ),
    )
    result = await _user_flow(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_config_flow_unreachable_portal_is_cannot_connect(
    hass, socket_enabled, nbpower_urls, patched_helper_session,
    enable_custom_integrations, caplog,
):
    closed = TestServer(web.Application())
    await closed.start_server()
    base = nb.server_base(closed)
    await closed.close()
    nbpower_urls(base)
    result = await _user_flow(hass)
    assert result["errors"] == {"base": "cannot_connect"}
    assert "Could not reach NB Power while signing in" in caplog.text


async def test_config_flow_unexpected_error_is_unknown(
    hass, nbpower_urls, patched_helper_session, enable_custom_integrations,
    monkeypatch, caplog,
):
    async def _boom(self, username, password, *, validate=True):
        raise KeyError("ctl00")

    monkeypatch.setattr(NBPowerClient, "bootstrap", _boom)
    result = await _user_flow(hass)
    assert result["errors"] == {"base": "unknown"}
    assert "Unexpected error while signing in to NB Power" in caplog.text
