"""Diagnostics download for an NB Power entry.

What the integration knows about its own state, with the account kept
out: credentials, the session token, the account and meter numbers and
anything named after them are redacted, and the daily usage is reduced to
the days it covers (the counters and their ledgers stay: they are what a
statistics problem is debugged with). The sign-in history is the part to
look at when sign-ins get turned away.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .coordinator import ATTR_DAILY_KWH, async_stored_state, loaded_coordinator

# The entry's title and unique id are built from the account number.
TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    "title",
    "unique_id",
    "token",
    "account_number",
    "utility_account_number",
    "meter_number",
}


def _data_summary(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """The last refresh, with the daily usage reduced to the days it covers."""
    if not data:
        return None
    daily = data.get(ATTR_DAILY_KWH) or {}
    summary = {key: value for key, value in data.items() if key != ATTR_DAILY_KWH}
    for key in ("last_daily_date", "last_updated"):
        if summary.get(key) is not None:
            summary[key] = summary[key].isoformat()
    summary["daily_days"] = {
        "count": len(daily),
        "first": min(daily, default=None),
        "last": max(daily, default=None),
    }
    return summary


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry, set up or not."""
    integration = await async_get_integration(hass, DOMAIN)
    refresh: dict[str, Any] | None = None
    if coordinator := loaded_coordinator(entry):
        failure = coordinator.last_exception
        refresh = {
            "last_update_success": coordinator.last_update_success,
            "last_exception": (
                (str(failure) or type(failure).__name__) if failure else None
            ),
            "update_interval": str(coordinator.update_interval),
            "data": _data_summary(coordinator.data),
        }
    return async_redact_data(
        {
            "version": str(integration.version),
            "entry": {
                **entry.as_dict(),
                "state": entry.state.value,
                "reason": entry.reason,
            },
            "refresh": refresh,
            # Kept by an entry whose setup failed too, so it still says how
            # the sign-ins went.
            "stored": await async_stored_state(hass, entry),
        },
        TO_REDACT,
    )
