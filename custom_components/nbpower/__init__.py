"""The NB Power integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import NBPowerClient
from .const import (
    BASE_URL,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    WIDGET_API_URL,
)
from .coordinator import NBPowerCoordinator, async_remove_stored

PLATFORMS: list[str] = ["sensor"]

type NBPowerConfigEntry = ConfigEntry[NBPowerCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: NBPowerConfigEntry) -> bool:
    """Set up NB Power from a config entry."""
    if CONF_USERNAME not in entry.data or CONF_PASSWORD not in entry.data:
        # Pre-0.4 entries stored a browser-captured token; those tokens
        # expire within hours, so ask for credentials once via re-auth.
        raise ConfigEntryAuthFailed(
            "This entry uses an expired token; sign in with your username and password"
        )

    # A dedicated session: the portal login lives in cookies, and the shared
    # session's jar is common to every entry — two accounts logging in at
    # once could pick up each other's widget token. Detached on unload.
    client = NBPowerClient(
        async_create_clientsession(hass),
        base_url=BASE_URL,
        widget_api_url=WIDGET_API_URL,
    )
    coordinator = NBPowerCoordinator(
        hass,
        entry,
        client,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        scan_interval=timedelta(
            hours=entry.options.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL.total_seconds() / 3600
            )
        ),
    )
    await coordinator.async_load_stored()
    # Every refresh, this first one included, is followed by a background
    # pass that brings the statistics up to date (a few minutes the first
    # time, with a year of hourly data to fetch).
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # No update listener: the options flow reloads the entry itself, and so
    # do re-authentication and reconfiguration.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NBPowerConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: NBPowerConfigEntry) -> None:
    """Delete the stored progress of an entry that was removed.

    The statistics stay: they are the account's history, and an entry added
    again for the same account rebuilds them under the same ids.
    """
    await async_remove_stored(hass, entry)
