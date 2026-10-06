"""The NB Power integration."""

from __future__ import annotations

import logging
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
    DOMAIN,
    WIDGET_API_URL,
)
from .coordinator import NBPowerCoordinator
from .statistics import (
    async_backfill_hourly_statistics,
    async_import_history_statistics,
)

PLATFORMS: list[str] = ["sensor"]

type NBPowerConfigEntry = ConfigEntry[NBPowerCoordinator]

_LOGGER = logging.getLogger(__name__)


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
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Phase 1 (immediate): import the fetched history at day resolution. A
    # missing recorder (rare) must not break setup; the import retries on
    # restart.
    try:
        await async_import_history_statistics(hass, entry, coordinator)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Statistics backfill failed; will retry on restart", exc_info=True)

    # Phase 2 (background): upgrade recent history to hourly resolution from
    # 15-minute data, one paced request per day; resumable across restarts.
    # It always runs — the chain must reach the frontier the energy sensor
    # anchors to — and the backfill option only sets how far back it goes.
    # Starting it now, in the same boot as phase 1, seeds its chain from the
    # history phase 1 just imported.
    entry.async_create_background_task(
        hass,
        async_backfill_hourly_statistics(hass, entry, coordinator),
        f"{DOMAIN}_hourly_backfill_{entry.entry_id}",
    )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NBPowerConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: NBPowerConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
