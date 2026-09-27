"""Historical usage import into the recorder statistics table.

The Energy dashboard consumes hourly long-term statistics. Going forward
the ``total_increasing`` energy sensor generates them natively via the
recorder; this module backfills history for the same ``statistic_id`` in
two phases:

1. **Immediate (day resolution)**: all monthly billing cycles (~3 years,
   each cycle's kWh spread evenly across its days) plus the trailing daily
   window, imported during setup.
2. **Background (hour resolution)**: the last ~365 days are re-fetched as
   15-minute intervals and imported as hourly rows. Because
   ``async_import_statistics`` updates same-period rows in place, each
   day-row at midnight is replaced by that day's 24 hourly rows — no
   double-counting, and the process is resumable across restarts.

Imported periods always end before the entity's first native statistic
(setup moment), so imports never collide with recorder-generated rows.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .api import UsageRow
from .const import DOMAIN, INTERVAL_BACKFILL_DAYS, INTERVAL_REQUEST_PAUSE
from .coordinator import NBPowerCoordinator
from .exceptions import NBPowerError
from .sensor import ENERGY_KEY, UNIQUE_ID_TEMPLATE

_LOGGER = logging.getLogger(__name__)


def daily_points(rows: list[UsageRow], today: date) -> list[tuple[date, float]]:
    """Expand history rows into (day, kWh) points, skipping today.

    Monthly cycle rows are spread evenly across their FromDate..ToDate span;
    daily rows pass through as single points.
    """
    points: list[tuple[date, float]] = []
    for row in rows:
        if row.kwh is None:
            continue
        start = row.start.date()
        if row.end is not None:
            end = row.end.date()
            days = (end - start).days + 1
            if days <= 0:
                continue
            per_day = round(row.kwh / days, 4)
            points.extend((start + timedelta(days=i), per_day) for i in range(days))
        else:
            points.append((start, row.kwh))
    return [p for p in points if p[0] < today]


def hourly_points(rows: list[UsageRow]) -> list[tuple[datetime, float]]:
    """Aggregate 15-minute rows into (naive hour, kWh) points."""
    buckets: dict[datetime, float] = {}
    for row in rows:
        if row.kwh is None:
            continue
        hour = row.start.replace(minute=0, second=0, microsecond=0)
        buckets[hour] = buckets.get(hour, 0.0) + row.kwh
    return sorted((hour, round(kwh, 4)) for hour, kwh in buckets.items())


def _metadata(statistic_id: str) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=statistic_id,
        unit_class="energy",
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


def _rows_to_statistics(
    points: list[tuple[date | datetime, float]],
) -> list[StatisticData]:
    """Build statistic rows with a running-total state per period start.

    The recorder's convention for total sensors is a CUMULATIVE ``sum``:
    the Energy dashboard renders ``change`` — the difference of
    consecutive ``sum`` values — so each row's ``sum`` must be the running
    total *including* that period's usage (and ``state`` the total at the
    period's start). Writing per-period deltas makes every hour that used
    less than the previous one render negative.

    Points may be plain dates (day-resolution rows, mapped to local
    midnight) or naive datetimes (hour-resolution rows, localized as-is).
    """
    statistics: list[StatisticData] = []
    cumulative = 0.0
    for when, kwh in points:
        if isinstance(when, datetime):
            start = dt_util.as_local(when)
        else:
            start = dt_util.start_of_local_day(when)
        statistics.append(
            StatisticData(
                start=start,
                state=round(cumulative, 3),
                sum=round(cumulative + kwh, 3),
            )
        )
        cumulative += kwh
    return statistics


def _energy_statistic_id(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    registry = er.async_get(hass)
    return registry.async_get_entity_id(
        "sensor",
        DOMAIN,
        UNIQUE_ID_TEMPLATE.format(entry_id=entry.entry_id, key=ENERGY_KEY),
    )


async def async_import_history_statistics(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: NBPowerCoordinator
) -> None:
    """Phase 1: import the already-fetched history at day resolution."""
    if coordinator.stats_imported:
        return
    statistic_id = _energy_statistic_id(hass, entry)
    if statistic_id is None:
        _LOGGER.warning("Energy sensor not registered yet; will retry statistics import")
        return
    if not (history := coordinator.history_rows):
        _LOGGER.info("No usage history available; skipping statistics import")
        await coordinator.mark_stats_imported()
        return

    points = daily_points(history, dt_util.now().date())
    if points:
        async_import_statistics(hass, _metadata(statistic_id), _rows_to_statistics(points))
        _LOGGER.info(
            "Imported %s day-resolution statistics for %s", len(points), statistic_id
        )
    await coordinator.mark_stats_imported()


async def async_backfill_hourly_statistics(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: NBPowerCoordinator
) -> None:
    """Import hourly statistics for published days, resumable and repeatable.

    Runs once at setup for the historical backfill and again after every
    coordinator refresh: the 15-minute feed publishes a day's data only
    after that day ends, so each refresh picks up the newly published days
    (usually yesterday). Rows chain cumulative sums from the persisted
    seed; afterwards the sensor's monotonic floor is raised to the chain
    end and any native recorder rows after the chain are flattened onto it
    — without that, the energy of each newly imported day would also be
    recorded as one lump at the refresh hour (double count).
    """
    statistic_id = _energy_statistic_id(hass, entry)
    if statistic_id is None:
        return
    client = coordinator.client
    today = dt_util.now().date()
    start_day = today - timedelta(days=INTERVAL_BACKFILL_DAYS)
    through = coordinator.stored_interval_through()
    resume_from = max(start_day, through + timedelta(days=1)) if through else start_day

    # Seed the hourly state series where the day-resolution series left
    # off: everything phase 1 recorded strictly before the first hour row.
    seed = coordinator.stored_interval_state_seed()
    if seed is None:
        seed = 0.0
        if coordinator.history_rows:
            prior = [
                p for p in daily_points(coordinator.history_rows, today) if p[0] < resume_from
            ]
            seed = round(sum(kwh for _, kwh in prior), 3)
        await coordinator.store_interval_state_seed(seed)

    imported = 0
    day = resume_from
    last_imported_start: datetime | None = None
    while day < today:
        try:
            rows = await client.get_interval_usage(day)
        except (NBPowerError, TimeoutError, OSError) as err:
            _LOGGER.warning(
                "Hourly statistics import stopped at %s (%s); it will retry "
                "on the next refresh",
                day,
                err,
            )
            return

        if rows:
            points = hourly_points(rows)
            statistics: list[StatisticData] = []
            for when, kwh in points:
                start = dt_util.as_local(when)
                statistics.append(
                    StatisticData(
                        start=start,
                        state=round(seed, 3),
                        sum=round(seed + kwh, 3),
                    )
                )
                last_imported_start = start
                seed += kwh
            async_import_statistics(hass, _metadata(statistic_id), statistics)
            await coordinator.store_interval_state_seed(round(seed, 3))
            imported += len(points)
        else:
            # Days this close to now have not validated yet; stop here and
            # let a later run pick them up once the portal publishes them.
            if (today - day).days <= 14:
                _LOGGER.info(
                    "Hourly backfill reached unpublished data at %s "
                    "(imported %s hourly rows so far)",
                    day,
                    imported,
                )
                await coordinator.store_interval_through(day - timedelta(days=1))
                return

        await coordinator.store_interval_through(day)
        day += timedelta(days=1)
        await asyncio.sleep(INTERVAL_REQUEST_PAUSE)

    if imported:
        _LOGGER.info(
            "Hourly statistics import: %s new rows for %s (chain now %.1f)",
            imported,
            statistic_id,
            seed,
        )
        # The interval-fed chain can total more than the books-fed sensor
        # (the 15-minute feed runs ahead of the daily books). Raise the
        # sensor's floor to the chain end so the sensor continues the
        # imported series; the resulting sensor jump would otherwise be
        # recorded by the recorder as a lump at the refresh hour, double
        # counting the day that was just imported. Flatten the native rows
        # after the chain onto the new chain end to absorb it.
        await coordinator.raise_floor_to(seed)
        await _async_flatten_tail(hass, statistic_id, last_imported_start, round(seed, 3))


async def _async_flatten_tail(
    hass: HomeAssistant,
    statistic_id: str,
    after: datetime | None,
    chain_end: float,
) -> None:
    """Rewrite native rows after the imported chain to sit at ``chain_end``.

    The sensor's floor raise (and today's interval fold-in) create native
    state changes whose energy is already attributed to real hours by the
    imported chain; rewriting the tail rows to a flat ``chain_end`` removes
    the duplicate lumps while keeping the series continuous.
    """
    if after is None:
        return
    from homeassistant.components.recorder.statistics import get_last_statistics

    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 72, statistic_id, True, {"sum"}
    )
    tail = [r for r in (rows or []) if r["start"] > after]
    if not tail:
        return
    updates = [
        StatisticData(start=r["start"], state=chain_end, sum=chain_end) for r in tail
    ]
    async_import_statistics(hass, _metadata(statistic_id), updates)
