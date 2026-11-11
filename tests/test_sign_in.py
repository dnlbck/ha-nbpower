"""Sign-ins that fail without the password being wrong: waited out, not re-auth.

Live (2026-10/11): six times in five weeks a routine sign-in came back as
the login page, the integration took that for a wrong password and stopped
until the password was typed in again — about 40% of those weeks went by
that way. The one that was looked at (Nov 10/11) was scheduled maintenance:
the portal had its login form switched off for 29 hours, with a notice on
the page saying so.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import conftest as nb
from custom_components.nbpower.api import NBPowerClient
from custom_components.nbpower.const import (
    DOMAIN,
    SIGN_IN_DAILY_RETRIES,
    SIGN_IN_ERROR_DELAYS,
    SIGN_IN_EVENTS_KEPT,
    SIGN_IN_RETRY_DELAYS,
)
from custom_components.nbpower.diagnostics import async_get_config_entry_diagnostics
from custom_components.nbpower.exceptions import (
    NBPowerApiError,
    NBPowerAuthError,
    NBPowerSignInClosed,
    NBPowerSignInRejected,
)
from custom_components.nbpower.sign_in import ASK, GIVE_UP, RETRY, SignInGuard

FLOW_INPUT = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "s3cret"}
T0 = datetime(2026, 11, 10, 22, 42, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


# --- The pacing itself -----------------------------------------------------


def test_guard_waits_out_refusals_then_asks_then_gives_up():
    guard = SignInGuard()
    assert guard.wait_until(T0) is None
    now = T0
    for delay in SIGN_IN_RETRY_DELAYS:
        assert guard.rejected(now, "turned away") == RETRY
        assert guard.wait_until(now) == now + delay
        assert guard.wait_until(now + delay - timedelta(seconds=1)) == now + delay
        now += delay
        assert guard.wait_until(now) is None
    # A day in: ask for the credentials, yet keep trying daily for a week.
    assert now - T0 == DAY
    for _ in range(SIGN_IN_DAILY_RETRIES):
        assert guard.rejected(now, "turned away") == ASK
        assert guard.wait_until(now) == now + DAY
        now += DAY
    assert now - T0 == 7 * DAY
    assert guard.rejected(now, "turned away") == GIVE_UP
    # Nothing is scheduled any more: a reload may try (and give up) again.
    assert guard.wait_until(now) is None
    assert guard.rejected(now, "turned away") == GIVE_UP
    assert guard.rejected_since == T0


def test_guard_success_ends_the_refusals():
    guard = SignInGuard()
    guard.rejected(T0, "turned away")
    guard.rejected(T0 + HOUR, "turned away")
    assert guard.succeeded(T0 + 4 * HOUR) == 4 * HOUR
    assert (guard.rejections, guard.rejected_since, guard.retry_at) == (0, None, None)
    assert guard.last_failure is None
    assert guard.succeeded(T0 + 8 * HOUR) is None  # nothing to recover from
    # The next refusal starts from the first wait again.
    assert guard.rejected(T0 + 12 * HOUR, "turned away") == RETRY
    assert guard.wait_until(T0 + 12 * HOUR) == T0 + 12 * HOUR + SIGN_IN_RETRY_DELAYS[0]


def test_guard_portal_errors_are_not_held_against_the_credentials():
    guard = SignInGuard()
    now = T0
    for delay in (*SIGN_IN_ERROR_DELAYS, SIGN_IN_ERROR_DELAYS[-1]):
        guard.failed(now, "Login POST returned HTTP 503")
        assert guard.wait_until(now) == now + delay
        now += delay
    assert guard.rejections == 0 and guard.rejected_since is None
    assert guard.last_failure == "Login POST returned HTTP 503"
    assert guard.succeeded(now) is None
    guard.failed(now, "Login POST returned HTTP 503")
    assert guard.wait_until(now) == now + SIGN_IN_ERROR_DELAYS[0]
    # A refusal after errors is the first refusal.
    assert guard.rejected(now + HOUR, "turned away") == RETRY
    assert guard.errors == 0


def test_guard_survives_storage_and_a_clock_set_back():
    guard = SignInGuard()
    guard.rejected(T0, "turned away")
    restored = SignInGuard(json.loads(json.dumps(guard.as_dict())))
    assert restored.as_dict() == guard.as_dict()
    assert restored.wait_until(T0 + timedelta(minutes=30)) == T0 + SIGN_IN_RETRY_DELAYS[0]
    # With the clock a month back, the wait is the longest one, not a month.
    early = T0 - 30 * DAY
    assert restored.wait_until(early) == early + DAY
    # Whatever is in storage, loading it does not fail.
    assert SignInGuard({"retry_at": "soon", "events": ["?"]}).wait_until(T0) is None


def test_guard_keeps_a_bounded_history():
    guard = SignInGuard()
    for hours in range(SIGN_IN_EVENTS_KEPT + 5):
        guard.succeeded(T0 + hours * HOUR)
    guard.rejected(T0 + 100 * HOUR, "x" * 1000)
    assert len(guard.events) == SIGN_IN_EVENTS_KEPT
    assert guard.events[-2] == {
        "at": (T0 + (SIGN_IN_EVENTS_KEPT + 4) * HOUR).isoformat(),
        "result": "ok",
        "detail": None,
    }
    assert guard.events[-1]["result"] == "rejected"
    assert len(guard.events[-1]["detail"]) == 300


# --- What the client makes of the portal's answer --------------------------


async def _sign_in(portal, password="s3cret") -> None:
    """Sign in to the mock portal; a failed sign-in leaves nothing behind."""
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        client = NBPowerClient(
            session, base_url=nb.server_base(portal), widget_api_url=nb.server_base(portal)
        )
        try:
            await client.bootstrap("user@example.com", password)
        except Exception:
            assert not client.bootstrapped
            raise
        assert client.bootstrapped


async def test_login_form_switched_off_is_not_posted_to(portal):
    """Live: for scheduled maintenance the portal disables its login form
    and says from when until when. Posted anyway the form just came back,
    which read as a wrong password."""
    portal.app["state"]["maintenance"] = True
    with pytest.raises(NBPowerSignInClosed) as raised:
        await _sign_in(portal)
    assert raised.value.notice == nb.MAINTENANCE_NOTICE
    assert "until Wednesday, November 11 at 10:00pm" in str(raised.value)
    # No verdict on the credentials, and they never left.
    assert not isinstance(raised.value, NBPowerAuthError)
    assert portal.app["login_posts"] == []


async def test_refusal_carries_what_the_portal_said(portal):
    portal.app["state"]["reject_login"] = True
    with pytest.raises(NBPowerSignInRejected) as raised:
        await _sign_in(portal)
    # Its words about this sign-in, not the notice the page shows anyway.
    assert raised.value.reason == nb.REFUSED_ALERT
    assert not raised.value.challenged
    assert nb.SITE_NOTICE not in str(raised.value)


async def test_refusal_notes_a_word_verification(portal):
    portal.app["state"].update(reject_login=True, login_captcha=True)
    with pytest.raises(NBPowerSignInRejected) as raised:
        await _sign_in(portal)
    assert raised.value.challenged
    assert "word verification" in str(raised.value)


@pytest.mark.parametrize(
    ("alert", "reason"),
    [
        (" ", None),  # the login page again, without a word
        ("<script>alert('Login failed. Try later.');</script>", "Login failed. Try later."),
        (
            "<script>radalert('Your account is locked.', 330, 180, 'Login');</script>",
            "Your account is locked.",
        ),
        ("No user@example.com with password s3cret", "No … with password …"),
        ("<b>Account</b>   locked\n for 30 minutes", "Account locked for 30 minutes"),
    ],
)
async def test_refusal_reason_is_cleaned_up(portal, alert, reason):
    portal.app["state"]["reject_login"] = alert
    with pytest.raises(NBPowerSignInRejected) as raised:
        await _sign_in(portal)
    assert raised.value.reason == reason
    assert "s3cret" not in str(raised.value)
    # What the page was announcing anyway is passed on only for want of an
    # answer.
    assert raised.value.notice == nb.SITE_NOTICE
    assert (nb.SITE_NOTICE in str(raised.value)) == (reason is None)


async def test_wrong_password_is_a_refusal_with_the_portals_reason(portal):
    with pytest.raises(NBPowerSignInRejected) as raised:
        await _sign_in(portal, password="wrong")
    assert raised.value.reason == nb.WRONG_PASSWORD_ALERT


async def test_token_refused_after_signing_in_is_not_an_auth_error(portal):
    """The portal signed in and issued the token itself; the WidgetAPI
    refusing it says nothing about the credentials."""
    portal.app["state"]["verify_rejects"] = True
    with pytest.raises(NBPowerApiError, match="refused the token the portal just issued"):
        await _sign_in(portal)


# --- Home Assistant: a refused sign-in is waited out -----------------------


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"NB Power {nb.ACCOUNT_NUMBER}",
        data=FLOW_INPUT,
        options={"backfill_hourly": False},
        unique_id=f"account_{nb.ACCOUNT_NUMBER}",
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, portal, nbpower_urls) -> MockConfigEntry:
    nbpower_urls(nb.server_base(portal))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await nb.recorder_done(hass)
    return entry


async def _refused_setup(hass, portal, nbpower_urls) -> MockConfigEntry:
    """An entry whose first sign-in the portal turns away."""
    nbpower_urls(nb.server_base(portal))
    portal.app["state"]["reject_login"] = True
    entry = _entry(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _refresh(hass, coordinator) -> None:
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await nb.recorder_done(hass)


async def _refresh_after_the_wait(hass, coordinator) -> None:
    """Refresh as if the wait before the next sign-in had run out."""
    coordinator._sign_in.retry_at = dt_util.utcnow() - timedelta(seconds=1)
    await _refresh(hass, coordinator)


def _session_expires_and_sign_ins_are_refused(portal) -> None:
    portal.app["state"].update(expire_widget_token=True, reject_login=True)


def _reauth_flows(hass) -> list:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == "reauth"
    ]


async def test_maintenance_is_waited_out_without_asking_for_credentials(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    """The live case: the login form off for 29 hours. Up to 0.12 that
    ended in a request for the password, and nothing more until it came."""
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    posts = len(portal.app["login_posts"])
    portal.app["state"].update(expire_widget_token=True, maintenance=True)

    # However long the window is, and however often it gets looked at.
    for _ in range(20):
        await _refresh_after_the_wait(hass, coord)
        assert not coord.last_update_success
    assert not _reauth_flows(hass)
    assert len(portal.app["login_posts"]) == posts  # the credentials stay home
    assert nb.MAINTENANCE_NOTICE in str(coord.last_exception)
    assert coord.stored_state()["sign_in"]["rejections"] == 0
    # It looks again every two hours at most, without hurrying.
    wait = SIGN_IN_ERROR_DELAYS[-1]
    assert wait < coord.update_interval < wait + timedelta(minutes=1)

    portal.app["state"]["maintenance"] = False
    await _refresh_after_the_wait(hass, coord)
    assert coord.last_update_success
    assert len(portal.app["login_posts"]) == posts + 1


async def test_setup_during_maintenance_waits_and_says_why(
    recorder_mock, hass, hass_storage, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    nbpower_urls(nb.server_base(portal))
    portal.app["state"]["maintenance"] = True
    entry = _entry(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.reason.startswith("NB Power is not taking sign-ins")
    assert "until Wednesday, November 11 at 10:00pm" in entry.reason
    assert not _reauth_flows(hass)
    assert portal.app["login_posts"] == []

    # Back in by itself once the form is open again and the wait is over.
    portal.app["state"]["maintenance"] = False
    await _retry_setup_after_the_wait(hass, hass_storage, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert len(portal.app["login_posts"]) == 1


async def _retry_setup_after_the_wait(hass, hass_storage, entry) -> None:
    """Retry a failed setup, as Home Assistant does, with the stored wait
    before the next sign-in run out."""
    stored = hass_storage[f"nbpower.{entry.entry_id}"]["data"]["sign_in"]
    stored["retry_at"] = (dt_util.utcnow() - timedelta(seconds=1)).isoformat()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_refused_sign_in_is_waited_out_and_retried(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations, caplog,
):
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    posts = len(portal.app["login_posts"])
    _session_expires_and_sign_ins_are_refused(portal)

    await _refresh(hass, coord)
    assert not coord.last_update_success
    assert not _reauth_flows(hass)
    assert len(portal.app["login_posts"]) == posts + 1
    assert nb.REFUSED_ALERT in caplog.text
    assert FLOW_INPUT[CONF_PASSWORD] not in caplog.text
    # The next poll comes when the wait ends, not four hours on.
    wait = SIGN_IN_RETRY_DELAYS[0]
    assert wait < timedelta(hours=4)
    assert wait < coord.update_interval < wait + timedelta(minutes=1)

    # Until then the portal does not hear from it.
    await _refresh(hass, coord)
    assert len(portal.app["login_posts"]) == posts + 1
    assert "Waiting until" in str(coord.last_exception)

    portal.app["state"]["reject_login"] = False
    await _refresh_after_the_wait(hass, coord)
    assert coord.last_update_success
    assert len(portal.app["login_posts"]) == posts + 2
    assert coord.update_interval == timedelta(hours=4)
    sign_in = coord.stored_state()["sign_in"]
    assert (sign_in["rejections"], sign_in["retry_at"]) == (0, None)
    assert [e["result"] for e in sign_in["events"]] == ["ok", "rejected", "ok"]


async def test_a_day_of_refusals_asks_for_credentials_but_keeps_trying(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    _session_expires_and_sign_ins_are_refused(portal)
    for _ in SIGN_IN_RETRY_DELAYS:
        await _refresh_after_the_wait(hass, coord)
        assert not _reauth_flows(hass)

    await _refresh_after_the_wait(hass, coord)
    assert len(_reauth_flows(hass)) == 1
    # Not the end of it: the portal may come round by itself.
    assert not isinstance(coord.last_exception, ConfigEntryAuthFailed)
    assert coord.update_interval == timedelta(hours=4)

    portal.app["state"]["reject_login"] = False
    await _refresh_after_the_wait(hass, coord)
    assert coord.last_update_success
    assert not _reauth_flows(hass)  # the request is withdrawn


async def test_a_week_of_refusals_gives_up(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    posts = len(portal.app["login_posts"])
    _session_expires_and_sign_ins_are_refused(portal)
    for _ in range(len(SIGN_IN_RETRY_DELAYS) + SIGN_IN_DAILY_RETRIES):
        await _refresh_after_the_wait(hass, coord)
        assert not isinstance(coord.last_exception, ConfigEntryAuthFailed)

    await _refresh_after_the_wait(hass, coord)
    assert isinstance(coord.last_exception, ConfigEntryAuthFailed)
    assert len(_reauth_flows(hass)) == 1
    # Eleven sign-ins in a week, where retrying setups would post hundreds.
    assert len(portal.app["login_posts"]) == posts + 11


async def test_setup_during_a_refusal_waits_instead_of_hammering(
    recorder_mock, hass, hass_storage, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    entry = await _refused_setup(hass, portal, nbpower_urls)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert nb.REFUSED_ALERT in entry.reason
    assert not _reauth_flows(hass)
    assert len(portal.app["login_posts"]) == 1

    # Home Assistant retries a failed setup every few seconds to minutes.
    for _ in range(3):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "Waiting until" in entry.reason
    assert len(portal.app["login_posts"]) == 1

    # The wait over and the portal willing: the next retry gets in.
    portal.app["state"]["reject_login"] = False
    await _retry_setup_after_the_wait(hass, hass_storage, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert len(portal.app["login_posts"]) == 2


async def test_entering_the_credentials_again_ends_the_wait(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations, caplog,
):
    entry = await _refused_setup(hass, portal, nbpower_urls)
    portal.app["state"]["reject_login"] = False

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], FLOW_INPUT)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reauth_successful")
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.stored_state()["sign_in"]["rejections"] == 0
    # The refused one and the form's: the reloaded entry goes on with that.
    assert len(portal.app["login_posts"]) == 2
    # Home Assistant 2026.11 warned here: the flow and an update listener
    # both reloaded the entry.
    assert "update listener" not in caplog.text


async def test_reconfigure_signs_in_without_waiting(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    _session_expires_and_sign_ins_are_refused(portal)
    await _refresh(hass, coord)
    assert not coord.last_update_success
    portal.app["state"]["reject_login"] = False
    posts = len(portal.app["login_posts"])

    result = await entry.start_reconfigure_flow(hass)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "reconfigure")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], FLOW_INPUT)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (result["type"], result["reason"]) == (
        FlowResultType.ABORT,
        "reconfigure_successful",
    )
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not coord
    assert entry.runtime_data.last_update_success
    assert len(portal.app["login_posts"]) == posts + 1


async def test_reconfigure_rejects_a_different_account(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations, monkeypatch,
):
    from custom_components.nbpower import config_flow

    entry = await _setup(hass, portal, nbpower_urls)

    async def _other_account(hass, username, password):
        return "7654321", None

    monkeypatch.setattr(config_flow, "_validate_login", _other_account)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "someone@else.com", CONF_PASSWORD: "x"}
    )
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "wrong_account")
    assert entry.data == FLOW_INPUT


async def test_token_refused_after_signing_in_is_retried_soon(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    portal.app["state"].update(expire_widget_token=True, verify_rejects=True)
    await _refresh(hass, coord)
    assert not coord.last_update_success
    assert not _reauth_flows(hass)
    sign_in = coord.stored_state()["sign_in"]
    assert (sign_in["rejections"], sign_in["errors"]) == (0, 1)
    assert coord.update_interval < SIGN_IN_ERROR_DELAYS[0] + timedelta(minutes=1)

    portal.app["state"]["verify_rejects"] = False
    await _refresh_after_the_wait(hass, coord)
    assert coord.last_update_success


# --- No sign-in while a session will do ------------------------------------


async def test_restart_goes_on_with_the_stored_session(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup(hass, portal, nbpower_urls)
    assert len(portal.app["login_posts"]) == 1

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.last_update_success
    assert len(portal.app["login_posts"]) == 1

    # A token that expired in the meantime costs the usual one sign-in.
    portal.app["state"]["expire_widget_token"] = True
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.last_update_success
    assert len(portal.app["login_posts"]) == 2


async def test_setup_goes_on_with_the_config_flows_sign_in(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    """Checking the credentials and setting the entry up used to be two
    sign-ins within seconds."""
    nbpower_urls(nb.server_base(portal))
    result = await _user_flow(hass, FLOW_INPUT)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    coord = result["result"].runtime_data
    assert result["result"].state is ConfigEntryState.LOADED
    assert coord.last_update_success
    assert len(portal.app["login_posts"]) == 1
    # The sign-in history still has it.
    assert [e["result"] for e in coord.stored_state()["sign_in"]["events"]] == ["ok"]


# --- Config flow -----------------------------------------------------------


async def _user_flow(hass, user_input=None):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    if user_input is None:
        return result
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_config_flow_passes_on_what_the_portal_said(
    hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations, caplog,
):
    nbpower_urls(nb.server_base(portal))
    # The error text takes a {reason}; the form supplies it from the start.
    assert (await _user_flow(hass))["description_placeholders"] == {"reason": ""}

    portal.app["state"].update(reject_login=True, login_captcha=True)
    result = await _user_flow(hass, FLOW_INPUT)
    assert result["errors"] == {"base": "invalid_auth"}
    reason = result["description_placeholders"]["reason"]
    assert f'The portal said: "{nb.REFUSED_ALERT}"' in reason
    assert "word verification" in reason
    assert nb.REFUSED_ALERT in caplog.text
    assert FLOW_INPUT[CONF_PASSWORD] not in caplog.text


async def test_config_flow_during_maintenance_says_so(
    hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations,
):
    nbpower_urls(nb.server_base(portal))
    portal.app["state"]["maintenance"] = True
    result = await _user_flow(hass, FLOW_INPUT)
    assert result["errors"] == {"base": "sign_in_closed"}
    assert (
        f'The portal says: "{nb.MAINTENANCE_NOTICE}"'
        in result["description_placeholders"]["reason"]
    )
    assert portal.app["login_posts"] == []


async def test_config_flow_token_refusal_is_a_portal_error(
    hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations,
):
    nbpower_urls(nb.server_base(portal))
    portal.app["state"]["verify_rejects"] = True
    result = await _user_flow(hass, FLOW_INPUT)
    assert result["errors"] == {"base": "portal_error"}


async def test_changing_options_reloads_the_entry(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    assert not entry.update_listeners

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scan_interval": 6, "backfill_hourly": False}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.runtime_data is not coord
    assert entry.runtime_data.update_interval == timedelta(hours=6)


# --- Diagnostics and clean-up ----------------------------------------------


def _assert_account_kept_out(diagnostics: dict) -> None:
    dump = json.dumps(diagnostics, default=str)
    for secret in (
        FLOW_INPUT[CONF_USERNAME],
        FLOW_INPUT[CONF_PASSWORD],
        nb.ACCOUNT_NUMBER,
        nb.UTILITY_ACCOUNT_NUMBER,
        nb.WIDGET_TOKEN,
    ):
        assert secret not in dump


async def test_diagnostics_keep_the_account_out(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup(hass, portal, nbpower_urls)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    _assert_account_kept_out(diagnostics)
    assert diagnostics["entry"]["state"] == "loaded"
    assert diagnostics["refresh"]["last_update_success"] is True
    assert diagnostics["refresh"]["data"]["daily_days"]["count"] > 20
    assert "daily_kwh" not in diagnostics["refresh"]["data"]
    assert diagnostics["stored"]["sign_in"]["events"][-1]["result"] == "ok"
    assert diagnostics["stored"]["session"]["token"] == "**REDACTED**"
    assert diagnostics["stored"]["external"]["through"]


async def test_diagnostics_of_an_entry_that_cannot_sign_in(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    """The download is for exactly this moment, with the entry not set up."""
    entry = await _refused_setup(hass, portal, nbpower_urls)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    _assert_account_kept_out(diagnostics)
    assert diagnostics["entry"]["state"] == "setup_retry"
    assert diagnostics["refresh"] is None
    last = diagnostics["stored"]["sign_in"]["events"][-1]
    assert last["result"] == "rejected" and nb.REFUSED_ALERT in last["detail"]


async def test_removing_the_entry_deletes_its_stored_progress(
    recorder_mock, hass, hass_storage, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    entry = await _setup(hass, portal, nbpower_urls)
    key = f"nbpower.{entry.entry_id}"
    assert key in hass_storage
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


def test_strings_match_the_english_translation():
    component = nb.REPO_ROOT / "custom_components" / "nbpower"
    strings = json.loads((component / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (component / "translations" / "en.json").read_text(encoding="utf-8")
    )
    assert strings == english
