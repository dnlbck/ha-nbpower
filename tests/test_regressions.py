"""Regression tests: recovery paths, and what the portal gets asked."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import conftest as nb
from custom_components.nbpower import config_flow
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
    await nb.recorder_done(hass)
    return entry


def _eid(hass, entry, key="energy_usage"):
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")


async def _refresh(hass, coordinator):
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await nb.recorder_done(hass)


# What www.nbpower.com answers, with HTTP 403, from a country it does not
# serve (live, 2026-11, through a VPN exit abroad).
GEO_BLOCK_PAGE = (
    "<html><head><title>Access Unavailable / Accès non disponible</title></head>"
    "<body><h1>Access Unavailable</h1><p>Sorry, this website is not available "
    "from your current location.</p></body></html>"
)


@pytest.fixture
async def faulty_portal(socket_enabled):
    """The mock portal with switchable faults (login outage, rejected usage,
    a replacement account summary page)."""
    app = nb.build_app()

    @web.middleware
    async def faults(request, handler):
        state = app["state"]
        if state.get("geo_blocked"):
            return web.Response(status=403, text=GEO_BLOCK_PAGE, content_type="text/html")
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
        if state.get("usage_500") and request.path.endswith("GetUsageGeneration"):
            return web.Response(status=500, text="Internal Server Error")
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

    # Maintenance over, and so is the pause the failed sign-in called for.
    faulty_portal.app["state"]["login_503"] = False
    entry.runtime_data._sign_in.retry_at = None
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


async def test_rejected_fresh_session_is_not_signed_in_again_at_once(
    recorder_mock, hass, faulty_portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    """Home Assistant retries a failed setup within seconds, and each try
    used to be another sign-in."""
    nbpower_urls(nb.server_base(faulty_portal))
    faulty_portal.app["state"]["reject_usage"] = True
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="NB Power 1234567",
        data=FLOW_INPUT,
        unique_id=f"account_{nb.ACCOUNT_NUMBER}",
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for _ in range(3):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is entry.state.SETUP_RETRY
    assert len(faulty_portal.app["login_posts"]) == 1
    assert not _reauth_flows(hass)


async def test_usage_errors_do_not_cause_repeated_sign_ins(
    recorder_mock, hass, faulty_portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup(hass, faulty_portal, nbpower_urls, backfill_hourly=False)
    posts = faulty_portal.app["login_posts"]
    assert len(posts) == 1
    faulty_portal.app["state"]["usage_500"] = True

    # Running: the session was working, so it is kept and simply retried.
    await _refresh(hass, entry.runtime_data)
    assert not entry.runtime_data.last_update_success
    assert len(posts) == 1

    # A restart does not stay with a stored session that draws an error:
    # one new sign-in, then none at the pace the setup is retried.
    for _ in range(3):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is entry.state.SETUP_RETRY
    assert len(posts) == 2
    assert not _reauth_flows(hass)


async def test_reauth_rejects_a_different_account(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations, monkeypatch,
):
    entry = await _setup(hass, portal, nbpower_urls, backfill_hourly=False)

    async def _other_account(hass, username, password):
        return "7654321", None

    monkeypatch.setattr(config_flow, "_validate_login", _other_account)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "someone@else.com", CONF_PASSWORD: "x"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data == FLOW_INPUT


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
    # Each wrote its own statistics through to yesterday.
    assert all(entry.runtime_data.external_state()["through"] for entry in entries)


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


async def test_config_flow_names_a_refused_location(
    hass, faulty_portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations, caplog,
):
    """A page that is not the portal's is reported with its status and
    title, not as "site changed?"."""
    nbpower_urls(nb.server_base(faulty_portal))
    faulty_portal.app["state"]["geo_blocked"] = True
    result = await _user_flow(hass)
    assert result["errors"] == {"base": "portal_error"}
    assert "HTTP 403, title 'Access Unavailable / Accès non disponible'" in caplog.text
    assert not faulty_portal.app["login_posts"]


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
