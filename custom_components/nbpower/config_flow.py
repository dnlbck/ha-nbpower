"""Config flow for the NB Power integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .api import NBPowerClient
from .const import (
    BASE_URL,
    CONF_BACKFILL_HOURLY,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    WIDGET_API_URL,
)
from .coordinator import async_credentials_accepted, async_hand_over_session
from .exceptions import (
    NBPowerAuthError,
    NBPowerError,
    NBPowerSignInClosed,
    NBPowerSignInRejected,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class _SignInFailed(Exception):
    """A failed sign-in as the form reports it.

    ``key`` is the form's error key ("invalid_auth", "sign_in_closed",
    "cannot_connect", "portal_error" or "unknown") and ``said`` what the
    portal itself gave as the reason, worded for the form (empty when it
    gave none).
    """

    def __init__(self, key: str, said: str = "") -> None:
        super().__init__(key)
        self.key = key
        self.said = said


def _what_the_portal_said(err: NBPowerError) -> str:
    """The portal's own account of a sign-in it did not take, for the form."""
    said = ""
    if isinstance(err, NBPowerSignInClosed):
        if err.notice:
            said += f' The portal says: "{err.notice}".'
    elif isinstance(err, NBPowerSignInRejected):
        if err.reason:
            said += f' The portal said: "{err.reason}".'
        elif err.notice:
            said += f' Its login page carries the notice: "{err.notice}".'
        if err.challenged:
            said += (
                " Its login page is asking for a word verification (CAPTCHA),"
                " which the integration cannot answer."
            )
    return said


async def _validate_login(
    hass: HomeAssistant, username: str, password: str
) -> tuple[str, dict[str, Any] | None]:
    """Log in to the portal end-to-end; return (account number, session).

    Runs the full bootstrap: ASP.NET login, widget-token scrape, account
    resolution and a usage-request validation. The session is the sign-in
    itself, for the entry's setup to go on with. Raises _SignInFailed on
    failure; the cause is logged, as the form shows little more than the
    error key.
    """
    # Its own cookie jar, so this login cannot replace the portal session of
    # an entry that is already running.
    client = NBPowerClient(
        async_create_clientsession(hass),
        base_url=BASE_URL,
        widget_api_url=WIDGET_API_URL,
    )
    try:
        await client.bootstrap(username, password)
    except NBPowerAuthError as err:
        # Not necessarily a wrong password, so what the portal said is
        # worth having in the log.
        _LOGGER.warning("Sign-in not accepted: %s", err)
        raise _SignInFailed("invalid_auth", _what_the_portal_said(err)) from err
    except NBPowerSignInClosed as err:
        # Scheduled maintenance: the portal's login form is switched off.
        _LOGGER.warning("Sign-in not possible: %s", err)
        raise _SignInFailed("sign_in_closed", _what_the_portal_said(err)) from err
    except (TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.warning(
            "Could not reach NB Power while signing in: %s: %s",
            type(err).__name__,
            err,
        )
        raise _SignInFailed("cannot_connect") from err
    except NBPowerError as err:
        # Reached the portal, but a step of the sign-in came back in a shape
        # the client does not handle (site change, account-specific page).
        _LOGGER.warning("NB Power sign-in failed: %s", err)
        raise _SignInFailed("portal_error") from err
    except Exception as err:
        _LOGGER.exception("Unexpected error while signing in to NB Power")
        raise _SignInFailed("unknown") from err
    return str(client.account_number), client.session_state()


class NBPowerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the NB Power config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        said = ""
        if user_input is not None:
            try:
                account_number, session = await _validate_login(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except _SignInFailed as err:
                errors["base"], said = err.key, err.said
            else:
                unique_id = f"account_{account_number}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                async_hand_over_session(self.hass, unique_id, session)
                return self.async_create_entry(
                    title=f"NB Power {account_number}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={"reason": said},
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when stored credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_credentials(
            "reauth_confirm", self._get_reauth_entry(), user_input
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the credentials, or sign in again without waiting."""
        return await self._async_step_credentials(
            "reconfigure", self._get_reconfigure_entry(), user_input
        )

    async def _async_step_credentials(
        self, step_id: str, entry: ConfigEntry, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Take credentials for an existing entry and reload it with them."""
        errors: dict[str, str] = {}
        said = ""
        if user_input is not None:
            try:
                account_number, session = await _validate_login(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except _SignInFailed as err:
                errors["base"], said = err.key, err.said
            else:
                # Another account's credentials would silently re-point this
                # entry and append its usage to this account's statistics.
                unique_id = f"account_{account_number}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                # The portal takes these: the reloaded entry does not wait
                # out earlier refusals, and goes on with this sign-in.
                await async_credentials_accepted(self.hass, entry)
                async_hand_over_session(self.hass, unique_id, session)
                return self.async_update_reload_and_abort(entry, data=user_input)
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={"reason": said},
        )

    @staticmethod
    def async_get_options_flow(config_entry) -> NBPowerOptionsFlow:
        """Create the options flow."""
        return NBPowerOptionsFlow()


class NBPowerOptionsFlow(OptionsFlowWithReload):
    """Allow tweaking the polling interval and backfill behavior.

    Changed options reload the entry (the flow does that itself; an update
    listener doing it as well reloaded twice after a re-authentication).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=round(
                            options.get(
                                CONF_SCAN_INTERVAL,
                                DEFAULT_SCAN_INTERVAL.total_seconds() / 3600,
                            )
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=24, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(
                        CONF_BACKFILL_HOURLY,
                        default=options.get(CONF_BACKFILL_HOURLY, True),
                    ): bool,
                }
            ),
        )
