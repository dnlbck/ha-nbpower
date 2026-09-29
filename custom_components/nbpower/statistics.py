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
from .sensor import COST_KEY, ENERGY_KEY, UNIQUE_ID_TEMPLATE

_LOGGER = logging.getLogger(__name__)

# Only one statistics import may run at a time: the setup backfill and the
# per-refresh extension both drive the same chain, and interleaved runs
# corrupt the cumulative sums (observed live as a mixed-baseline series).
_import_lock = asyncio.Lock()


def daily_points(
    rows: list[UsageRow], today: date, value=lambda r: r.kwh
) -> list[tuple[date, float]]:
    """Expand history rows into (day, value) points, skipping today.

    Monthly cycle rows are spread evenly across their FromDate..ToDate span;
    daily rows pass through as single points.
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


def _cost_metadata(statistic_id: str) -> StatisticMetaData:
    """Mirror the recorder's own metadata for the monetary total sensor."""
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=statistic_id,
        unit_class=None,
        unit_of_measurement="CAD",
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

    today = dt_util.now().date()
    points = daily_points(history, today)
    if points:
        async_import_statistics(hass, _metadata(statistic_id), _rows_to_statistics(points))
        _LOGGER.info(
            "Imported %s day-resolution statistics for %s", len(points), statistic_id
        )

    # Cost history: cycles and daily rows carry dollar amounts; the cost
    # entity's books are exactly these inputs, so the imported chain ends
    # at the sensor's own value — no seam alignment needed.
    cost_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, UNIQUE_ID_TEMPLATE.format(entry_id=entry.entry_id, key=COST_KEY)
    )
    if cost_id:
        cost_points = [
            p
            for p in daily_points(history, today, value=lambda r: r.amount)
            if p[1] is not None
        ]
        if cost_points:
            cost_stats = _rows_to_statistics(cost_points)
            async_import_statistics(hass, _cost_metadata(cost_id), cost_stats)
            _LOGGER.info(
                "Imported %s day-resolution cost statistics for %s",
                len(cost_points),
                cost_id,
            )
            # The cost entity's native rows (recorder-seeded) may sit on a
            # zeroed baseline; align them onto the imported chain end.
            current_cost = (coordinator.data or {}).get("cumulative_cost")
            if current_cost is not None:
                await _async_align_native_block(
                    hass,
                    cost_id,
                    cost_stats[-1]["start"],
                    round(current_cost, 2),
                )
    await coordinator.mark_stats_imported()


async def async_backfill_hourly_statistics(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: NBPowerCoordinator
) -> None:
    """Import hourly statistics for published days, resumable and repeatable."""
    if _import_lock.locked():
        _LOGGER.debug("Statistics import already running; skipping")
        return
    async with _import_lock:
        await _async_import_hourly(hass, entry, coordinator)


async def _async_import_hourly(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: NBPowerCoordinator
) -> None:
    statistic_id = _energy_statistic_id(hass, entry)
    if statistic_id is None:
        return
    client = coordinator.client
    today = dt_util.now().date()
    start_day = today - timedelta(days=INTERVAL_BACKFILL_DAYS)
    through = coordinator.stored_interval_through()
    resume_from = max(start_day, through + timedelta(days=1)) if through else start_day
    # The recorder owns everything from the frontier onward; the import
    # never writes those hours (rewrites there get reverted and create
    # negative seams). On first completion the frontier is pinned to now.
    frontier = coordinator.stored_stats_frontier()
    day_limit = min(frontier.date(), today) if frontier else today

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
    while day < day_limit:
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
            day_seed = seed
            for when, kwh in points:
                start = dt_util.as_local(when)
                statistics.append(
                    StatisticData(
                        start=start,
                        state=round(day_seed, 3),
                        sum=round(day_seed + kwh, 3),
                    )
                )
                last_imported_start = start
                day_seed += kwh
            async_import_statistics(hass, _metadata(statistic_id), statistics)
            imported += len(points)
            if len(points) >= 24:
                # Complete day: advance the persisted chain; the next run
                # continues from here.
                seed = day_seed
                await coordinator.store_interval_state_seed(round(seed, 3))
            else:
                # The portal publishes days partially. This is the feed's
                # frontier: keep the persisted chain at the last complete
                # day (so this day re-fetches from the same base,
                # idempotently) and stop — later days cannot be trusted
                # to be complete either.
                _LOGGER.info(
                    "Day %s partially published (%s of 24 hours); will re-fetch",
                    day,
                    len(points),
                )
                break

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
    if frontier is None and coordinator.stored_interval_through() is not None:
        await coordinator.store_stats_frontier(dt_util.now())
        _LOGGER.info("Statistics frontier pinned; recorder owns hours from here")
    # Align the recorder-owned blocks after the chain onto the chain ends
    # (a no-op once aligned; re-anchors the native block if it drifts —
    # the recorder's own baseline can restart at zero after restarts).
    if coordinator.stored_interval_through() is not None:
        await _async_align_native_block(
            hass, statistic_id, last_imported_start, round(seed, 3)
        )
        cost_id = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, UNIQUE_ID_TEMPLATE.format(entry_id=entry.entry_id, key=COST_KEY)
        )
        current_cost = (coordinator.data or {}).get("cumulative_cost")
        if cost_id and current_cost is not None and frontier is not None:
            await _async_align_native_block(
                hass, cost_id, frontier, round(current_cost, 2)
            )


def _row_ts(value: datetime | float) -> float:
    """Normalize a statistics row start (datetime or epoch) to epoch."""
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


def _row_dt(value: datetime | float) -> datetime:
    """Normalize a statistics row start to an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else dt_util.utc_from_timestamp(value.timestamp())
    return dt_util.utc_from_timestamp(value)


async def _async_align_native_block(
    hass: HomeAssistant,
    statistic_id: str,
    after: datetime | None,
    chain_end: float,
) -> None:
    """Shift native recorder rows after the imported chain onto ``chain_end``.

    The recorder owns recent hours (its short-term data wins over row
    rewrites), so alignment goes through the recorder's own
    ``adjust_statistics`` — it updates long- AND short-term rows, keeping
    future recompiles consistent. Without this, the first native row after
    the imported chain sits at the books-fed sensor level, below the
    interval-fed chain, rendering as a negative bar at the seam.
    """
    if after is None:
        return
    from homeassistant.components.recorder.statistics import (
        adjust_statistics,
        get_last_statistics,
    )

    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 72, statistic_id, True, {"sum"}
    )
    all_rows = sorted(
        (result.get(statistic_id) or []), key=lambda r: _row_ts(r["start"])
    )
    tail = [r for r in all_rows if _row_ts(r["start"]) > after.timestamp()]
    if not tail:
        return
    # Anchor the running level to the actual last row at or before the
    # walk window — the chain's last WRITTEN row (a partial day can sit
    # above the persisted complete-days seed).
    prior = [r for r in all_rows if _row_ts(r["start"]) <= after.timestamp()]
    level = round(prior[-1].get("sum") or 0.0, 3) if prior else chain_end

    # Walk the native block from the chain end, repairing every interior
    # dip. Restarts make the recorder recompile recent hours from its own
    # baseline, which can re-open seams anywhere in the block; a single
    # base shift is not enough. Sensor increases are preserved (the
    # running level simply rises); only decreases are pinned back up.
    instance = get_instance(hass)
    for row in tail:
        row_sum = row.get("sum") or 0.0
        deficit = round(level - row_sum, 3)
        if deficit > 0.01:
            await instance.async_add_executor_job(
                adjust_statistics,
                instance,
                statistic_id,
                _row_dt(row["start"]),
                deficit,
                "kWh",
            )
            _LOGGER.info(
                "Repaired native statistics dip for %s at %s (+%.3f)",
                statistic_id,
                row["start"],
                deficit,
            )
        elif row_sum > level:
            level = row_sum
