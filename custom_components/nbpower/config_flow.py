"""Config flow for the NB Power integration."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
from .exceptions import NBPowerAuthError, NBPowerError

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_login(hass: HomeAssistant, username: str, password: str) -> str:
    """Log in to the portal end-to-end; return the account number.

    Runs the full bootstrap: ASP.NET login, widget-token scrape, account
    resolution and a usage-request validation. Raises
    ValueError("invalid_auth"|"cannot_connect") on failure.
    """
    client = NBPowerClient(
        async_get_clientsession(hass), base_url=BASE_URL, widget_api_url=WIDGET_API_URL
    )
    try:
        await client.bootstrap(username, password)
    except NBPowerAuthError as err:
        raise ValueError("invalid_auth") from err
    except (NBPowerError, TimeoutError, aiohttp.ClientError) as err:
        raise ValueError("cannot_connect") from err
    return str(client.account_number)


class NBPowerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the NB Power config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            account_number = None
            try:
                account_number = await _validate_login(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except ValueError as err:
                errors["base"] = str(err.args[0])
            if account_number:
                await self.async_set_unique_id(f"account_{account_number}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"NB Power {account_number}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when stored credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        existing_entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                await _validate_login(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except ValueError as err:
                errors["base"] = str(err.args[0])
            if not errors:
                return self.async_update_reload_and_abort(
                    existing_entry, data=user_input
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=existing_entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry) -> NBPowerOptionsFlow:
        """Create the options flow."""
        return NBPowerOptionsFlow()


class NBPowerOptionsFlow(OptionsFlow):
    """Allow tweaking the polling interval and backfill behavior."""

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
