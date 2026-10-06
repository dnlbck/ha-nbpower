"""Hourly usage and cost as external statistics for the Energy dashboard.

The portal publishes usage a day or more after it happens. The energy
sensor's own statistics belong to the recorder from the statistics
frontier on, so there late usage can only appear as a step at the poll
that picks it up, never in the hour it was used. These two external
statistics, ``nbpower:energy_<account>`` and ``nbpower:cost_<account>``,
are written by the integration alone, so every hour carries its own usage
once the portal publishes it:

* before the hourly window, day resolution from the books (billing cycles
  spread across their days, then the daily window), written once;
* the hourly window (a year, or ``INTERVAL_RECENT_DAYS`` with the backfill
  option off) from ``Mode=H``, one paced request per day, resumable;
* on every refresh, each day after the last complete one is fetched and
  rewritten, so blocks published out of order land in their own hours and
  the later rows' sums follow.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import UsageRow
from .const import (
    CONF_BACKFILL_HOURLY,
    CURRENCY,
    DOMAIN,
    EXTERNAL_GEN,
    INTERVAL_BACKFILL_DAYS,
    INTERVAL_RECENT_DAYS,
    INTERVAL_REQUEST_PAUSE,
    MI_SINCE_DAYS,
)
from .coordinator import NBPowerCoordinator
from .exceptions import NBPowerError
from .statistics import daily_points

_LOGGER = logging.getLogger(__name__)


def account_key(entry: ConfigEntry, coordinator: NBPowerCoordinator) -> str:
    """The account number as a statistic object id fragment."""
    raw = (entry.unique_id or "").removeprefix("account_") or str(
        coordinator.client.account_number or ""
    )
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")


def _metadata(
    account: str, key: str, unit: str, unit_class: str | None
) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"NB Power {account} {key}",
        source=DOMAIN,
        statistic_id=f"{DOMAIN}:{key}_{account}",
        unit_class=unit_class,
        unit_of_measurement=unit,
    )


def hour_buckets(rows: list[UsageRow]) -> list[tuple[datetime, float, float]]:
    """Merge hourly rows into (local hour start, kWh, dollars) by instant.

    The portal labels every day 00:00-23:00. On the spring-forward day the
    02:00 label does not exist locally and resolves to the same instant as
    03:00, so the two merge; on the fall-back day the repeated hour has a
    single label. Day totals survive both.
    """
    buckets: dict[float, list[float]] = {}
    for row in rows:
        if row.kwh is None:
            continue
        hour = row.start.replace(minute=0, second=0, microsecond=0)
        bucket = buckets.setdefault(dt_util.as_local(hour).timestamp(), [0.0, 0.0])
        bucket[0] += row.kwh
        bucket[1] += row.amount or 0.0
    return [
        (dt_util.as_local(dt_util.utc_from_timestamp(ts)), kwh, cost)
        for ts, (kwh, cost) in sorted(buckets.items())
    ]


def _chain(
    points: list[tuple[datetime, float]], total: float
) -> tuple[list[StatisticData], float]:
    """Rows whose ``sum`` runs on from ``total``; returns them and the end.

    Each sum is rounded as it is carried forward, so a stored total resumes
    the chain exactly where the last written row ended (no sub-cent dips).
    """
    statistics: list[StatisticData] = []
    for start, value in points:
        total = round(total + value, 3)
        statistics.append(StatisticData(start=start, state=total, sum=total))
    return statistics, total


async def async_update_external_statistics(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: NBPowerCoordinator
) -> None:
    """Start the chain once, then extend it through the newest published hour.

    Runs under the entry's statistics lock (see statistics.py), after a
    refresh has fetched the books.
    """
    if "recorder" not in hass.config.components:
        return
    if not (account := account_key(entry, coordinator)):
        return
    energy_meta = _metadata(account, "energy", UnitOfEnergy.KILO_WATT_HOUR, "energy")
    cost_meta = _metadata(account, "cost", CURRENCY, None)
    today = dt_util.now().date()

    state = coordinator.external_state()
    if coordinator.external_history_pending:
        state = await _async_start_chain(
            hass, entry, coordinator, energy_meta, cost_meta, today
        )

    through = date.fromisoformat(state["through"])
    energy_total: float = state["energy_sum"]
    cost_total: float = state["cost_sum"]
    contiguous = True
    written = 0
    day = through + timedelta(days=1)
    while day <= today:
        try:
            rows = await coordinator.client.get_hourly_usage(day)
        except (NBPowerError, TimeoutError, aiohttp.ClientError, OSError) as err:
            _LOGGER.warning(
                "Hourly statistics stopped at %s (%s); they resume on the next "
                "refresh",
                day,
                err,
            )
            break
        if hours := hour_buckets(rows):
            energy_rows, energy_total = _chain(
                [(start, kwh) for start, kwh, _cost in hours], energy_total
            )
            cost_rows, cost_total = _chain(
                [(start, cost) for start, _kwh, cost in hours], cost_total
            )
            async_add_external_statistics(hass, energy_meta, energy_rows)
            async_add_external_statistics(hass, cost_meta, cost_rows)
            written += len(hours)
        # A day is final with all 24 hour labels in, or once it is old
        # enough that whatever is still missing is a permanent gap.
        final = (
            len({row.start.hour for row in rows if row.kwh is not None}) >= 24
            or (today - day).days > MI_SINCE_DAYS
        )
        if contiguous and final:
            state.update(
                through=day.isoformat(), energy_sum=energy_total, cost_sum=cost_total
            )
            await coordinator.async_store_external_state(state)
        else:
            # The stored chain stays at the last final day: this day and the
            # ones after it are fetched and rewritten on every pass until
            # they are final (blocks publish out of order).
            contiguous = False
        day += timedelta(days=1)
        if day <= today:
            await asyncio.sleep(INTERVAL_REQUEST_PAUSE)

    if written:
        _LOGGER.debug(
            "Hourly statistics for %s: wrote %s hours (final through %s)",
            energy_meta["statistic_id"],
            written,
            state["through"],
        )


async def _async_start_chain(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: NBPowerCoordinator,
    energy_meta: StatisticMetaData,
    cost_meta: StatisticMetaData,
    today: date,
) -> dict[str, Any]:
    """Clear both statistics and write the history before the hourly window."""
    window = (
        INTERVAL_BACKFILL_DAYS
        if entry.options.get(CONF_BACKFILL_HOURLY, True)
        else INTERVAL_RECENT_DAYS
    )
    window_start = today - timedelta(days=window)
    # A fresh chain: rows left by an earlier generation, or by a removed
    # entry for the same account, would break its sums. Queued on the
    # recorder ahead of the imports below.
    get_instance(hass).async_clear_statistics(
        [energy_meta["statistic_id"], cost_meta["statistic_id"]]
    )

    history = coordinator.history_rows

    def days(value: Callable[[UsageRow], float | None]) -> list[tuple[datetime, float]]:
        return [
            (dt_util.start_of_local_day(day), amount)
            for day, amount in daily_points(history, window_start, value=value)
        ]

    energy_rows, energy_total = _chain(days(lambda row: row.kwh), 0.0)
    cost_rows, cost_total = _chain(days(lambda row: row.amount), 0.0)
    if energy_rows:
        async_add_external_statistics(hass, energy_meta, energy_rows)
    if cost_rows:
        async_add_external_statistics(hass, cost_meta, cost_rows)
    state = {
        "gen": EXTERNAL_GEN,
        "window_start": window_start.isoformat(),
        "through": (window_start - timedelta(days=1)).isoformat(),
        "energy_sum": energy_total,
        "cost_sum": cost_total,
    }
    await coordinator.async_store_external_state(state)
    _LOGGER.info(
        "Started %s and %s: %s days of history before %s; hourly data follows "
        "in the background",
        energy_meta["statistic_id"],
        cost_meta["statistic_id"],
        len(energy_rows),
        window_start,
    )
    return state
