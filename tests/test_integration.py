"""Home Assistant-level tests: setup, sensors, config flow."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
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
    _monthly_cycles,
    recorder_done,
    server_base,
)
from custom_components.nbpower import external_statistics
from custom_components.nbpower.const import DOMAIN
from custom_components.nbpower.sensor import ENERGY_KEY

USERNAME = "user@example.com"
PASSWORD = "s3cret"
ACCOUNT_TITLE = "NB Power 1234567"
FLOW_INPUT = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


def _parse_day(text: str) -> date:
    return datetime.strptime(text, "%B %d, %Y").date()


def _books(key: str = "Consumption") -> float:
    """The books' sum: every cycle, plus the daily rows after the last one."""
    monthly = _monthly_cycles()
    daily = [r for r in _daily_window() if r["Consumption"] != ""]
    last_cycle_end = max(_parse_day(r["ToDate"]) for r in monthly)
    post = [r for r in daily if _parse_day(r["UsageDate"]) > last_cycle_end]
    return sum(r[key] for r in monthly) + sum(r[key] for r in post)


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
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")


def _value(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> float:
    return float(hass.states.get(_entity_id(hass, entry, key)).state)


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
    assert set(energy.attributes) >= {"last_daily_date", "daily_kwh"}

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


async def test_counters_start_from_the_books(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Cycles plus the daily rows after the last one; the statistics taking
    over (they have, by the time the setup is done) does not step them."""
    entry = await _setup_entry(hass, portal, nbpower_urls)
    assert _value(hass, entry, ENERGY_KEY) == pytest.approx(_books(), abs=0.01)
    assert _value(hass, entry, "cost_usage") == pytest.approx(_books("Amount"), abs=0.01)
    stored = entry.runtime_data.stored_state()
    assert stored["energy_ledger"]["chain"] == stored["external"]["chain"]
    assert stored["cost_ledger"]["chain"] == stored["external"]["chain"]

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _value(hass, entry, ENERGY_KEY) == pytest.approx(_books(), abs=0.01)
    assert _value(hass, entry, "cost_usage") == pytest.approx(_books("Amount"), abs=0.01)


async def test_counters_on_the_books_never_decrease(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
    monkeypatch,
):
    """Before the statistics take over, a downward revision of the books is
    held, not reported: a decrease reads as a meter reset."""

    async def _not_yet(hass, entry, coordinator):
        """The statistics have not been written through today yet."""

    monkeypatch.setattr(external_statistics, "async_update_statistics", _not_yet)
    entry = await _setup_entry(hass, portal, nbpower_urls, backfill_hourly=False)
    assert entry.runtime_data.stored_state()["energy_ledger"] is None
    energy, cost = _value(hass, entry, ENERGY_KEY), _value(hass, entry, "cost_usage")

    portal.app["state"]["revise_down"] = True
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.runtime_data.last_update_success
    assert _value(hass, entry, ENERGY_KEY) == energy
    assert _value(hass, entry, "cost_usage") == cost


async def test_sensors_own_statistics_are_left_to_the_recorder(
    recorder_mock,
    hass,
    portal,
    nbpower_urls,
    patched_helper_session,
    enable_custom_integrations,
):
    """Up to 0.13 history was imported under the energy and cost sensors'
    own ids, and the recorder's rows shifted to fit. Nothing is written
    there any more: the statistics are the integration's own two."""
    entry = await _setup_entry(hass, portal, nbpower_urls)
    await recorder_done(hass)
    sensors = {_entity_id(hass, entry, ENERGY_KEY), _entity_id(hass, entry, "cost_usage")}
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        dt_util.now(),
        sensors,
        "hour",
        None,
        {"sum"},
    )
    assert stats == {}
    # And the portal's 15-minute feed, which fed that import, is not asked.
    assert {call["Mode"] for call in portal.app["usage_calls"]} == {"M", "D", "H"}


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
    await hass.async_block_till_done(wait_background_tasks=True)
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
    """A stored password the portal no longer takes; re-auth recovers the entry."""
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
    # Turned away, which is not taken for proof of a wrong password at
    # once: the setup is retried, and re-authentication requested only if
    # it goes on (tests/test_sign_in.py). Entering the password works at
    # any point.
    assert entry.state is entry.state.SETUP_RETRY

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], FLOW_INPUT
    )
    await hass.async_block_till_done(wait_background_tasks=True)
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
    before = _value(hass, entry, ENERGY_KEY)

    # Kill the widget token: the next refresh must fail, re-login, retry.
    # (The mock mints a fresh token on successful login.)
    portal.app["state"]["expire_widget_token"] = True
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert entry.state is entry.state.LOADED
    assert entry.runtime_data.last_update_success
    assert _value(hass, entry, ENERGY_KEY) >= before


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
