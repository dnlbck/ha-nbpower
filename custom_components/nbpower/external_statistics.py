"""Hourly usage and cost as statistics for the Energy dashboard.

The portal publishes usage a day or more after it happens. Statistics the
recorder compiles from a sensor can only show such usage as a step at the
poll that picks it up, never in the hour it was used. So the integration
writes two (external) statistics of its own, ``nbpower:energy_<tag>`` and
``nbpower:cost_<tag>``, in which every hour carries its own usage once the
portal publishes it:

* before the hourly window, day resolution from the books (billing cycles
  spread across their days, then the daily window), written once;
* the hourly window (a year, or ``HOURLY_DAYS_WITHOUT_BACKFILL`` with the
  backfill option off) from ``Mode=H``, one paced request per day,
  resumable;
* on every refresh, each day after the last complete one is fetched and
  rewritten, so blocks published out of order land in their own hours and
  the later rows' sums follow.

They are the only statistics the integration writes, and its one ledger:
the two cumulative sensors follow the chains' totals. (Up to 0.13 the
energy sensor's own statistics were backfilled as well, from the 15-minute
feed, and the recorder's rows patched up to fit; the sensor ran on that
feed too.)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
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
    FINAL_AFTER_DAYS,
    HOURLY_DAYS,
    HOURLY_DAYS_WITHOUT_BACKFILL,
    REQUEST_PAUSE,
)
from .coordinator import NBPowerCoordinator
from .exceptions import NBPowerError

_LOGGER = logging.getLogger(__name__)

# One update per entry at a time: each continues the chain from the stored
# sums, and two interleaved would write rows from the same base. Keyed by
# entry, so accounts do not hold each other up, while a reloaded entry
# still waits for the run its unload is cancelling.
_locks: dict[str, asyncio.Lock] = {}


def statistic_ids(
    entry: ConfigEntry, coordinator: NBPowerCoordinator
) -> tuple[str, str] | None:
    """The (energy, cost) statistic ids for the entry's portal account.

    Keyed by a short hash of the portal's internal account id: stable
    across removing and re-adding the integration, while keeping the id
    itself out of statistic ids, which end up in screenshots and bug
    reports. (It is not a secret; the hash only keeps it out of view.)
    """
    account = (entry.unique_id or "").removeprefix("account_") or str(
        coordinator.client.account_number or ""
    )
    if not account:
        return None
    tag = hashlib.sha256(f"{DOMAIN}:{account}".encode()).hexdigest()[:8]
    return f"{DOMAIN}:energy_{tag}", f"{DOMAIN}:cost_{tag}"


def _metadata(
    statistic_id: str, name: str, unit: str, unit_class: str | None
) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_class=unit_class,
        unit_of_measurement=unit,
    )


def daily_points(
    rows: list[UsageRow],
    before: date,
    value: Callable[[UsageRow], float | None] = lambda row: row.kwh,
) -> list[tuple[date, float]]:
    """Expand the books' rows into (day, value) points for days before ``before``.

    Billing-cycle rows are spread evenly across their FromDate..ToDate
    span; daily rows pass through as single points.
    """
    points: list[tuple[date, float]] = []
    for row in rows:
        row_value = value(row)
        if row_value is None:
            continue
        start = row.start.date()
        if row.end is not None:
            end = row.end.date()
            days = (end - start).days + 1
            if days <= 0:
                continue
            per_day = round(row_value / days, 4)
            points.extend((start + timedelta(days=i), per_day) for i in range(days))
        else:
            points.append((start, row_value))
    return [point for point in points if point[0] < before]


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


async def async_update_statistics(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: NBPowerCoordinator
) -> None:
    """Bring the statistics up to date with what the portal has published.

    Runs as a background task after a refresh has fetched the books, so
    failures are logged here; the next refresh's run resumes from the
    stored progress.
    """
    async with _locks.setdefault(entry.entry_id, asyncio.Lock()):
        try:
            await _async_update(hass, entry, coordinator)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Statistics update failed; it will retry on the next refresh",
                exc_info=True,
            )


async def _async_update(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: NBPowerCoordinator
) -> None:
    """Start the chain once, then extend it through the newest published hour."""
    if "recorder" not in hass.config.components:
        return
    if not (ids := statistic_ids(entry, coordinator)):
        return
    energy_meta = _metadata(
        ids[0], "NB Power energy", UnitOfEnergy.KILO_WATT_HOUR, "energy"
    )
    cost_meta = _metadata(ids[1], "NB Power cost", CURRENCY, None)
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
    reached_today = True
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
            reached_today = False
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
            or (today - day).days > FINAL_AFTER_DAYS
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
            await asyncio.sleep(REQUEST_PAUSE)

    if written:
        _LOGGER.debug(
            "Hourly statistics for %s: wrote %s hours (final through %s)",
            energy_meta["statistic_id"],
            written,
            state["through"],
        )
    if reached_today:
        # The whole chain was walked, so its totals count every published
        # hour: the cumulative sensors follow them.
        await coordinator.async_statistics_totals(
            state.get("chain"), energy_total, cost_total
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
        HOURLY_DAYS
        if entry.options.get(CONF_BACKFILL_HOURLY, True)
        else HOURLY_DAYS_WITHOUT_BACKFILL
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
        # Names this chain: one started later has other sums, and the
        # sensors have to take it up afresh
        # (coordinator.async_statistics_totals).
        "chain": dt_util.utcnow().isoformat(),
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
