"""Historical usage import into the recorder statistics table.

The Energy dashboard consumes hourly long-term statistics. Going forward
the ``total_increasing`` energy sensor generates them natively via the
recorder; this module backfills history for the same ``statistic_id`` in
two phases:

1. **Immediate (day resolution)**: all monthly billing cycles (~3 years,
   each cycle's kWh spread evenly across its days) plus the trailing daily
   window, imported during setup.
2. **Background (hour resolution)**: the last ~365 days (only the last
   ``INTERVAL_RECENT_DAYS`` with the backfill option off) are re-fetched
   as 15-minute intervals and imported as hourly rows. Because
   ``async_import_statistics`` updates same-period rows in place, each
   day-row at midnight is replaced by that day's 24 hourly rows — no
   double-counting, and the process is resumable across restarts.

When the first hourly pass completes, a statistics frontier is pinned:
the recorder owns every hour from there on, imports never write at or
after it, and the recorder's own rows are aligned onto the imported chain.
Usage after the frontier therefore reaches this series only as the
sensor's steps; the hour-by-hour series for the Energy dashboard is the
external statistics in external_statistics.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta

import aiohttp

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
from .const import (
    CONF_BACKFILL_HOURLY,
    DOMAIN,
    INTERVAL_BACKFILL_DAYS,
    INTERVAL_RECENT_DAYS,
    INTERVAL_REQUEST_PAUSE,
    MI_SINCE_DAYS,
)
from .coordinator import NBPowerCoordinator
from .exceptions import NBPowerError
from .sensor import COST_KEY, ENERGY_KEY, UNIQUE_ID_TEMPLATE

_LOGGER = logging.getLogger(__name__)

# Only one statistics import may run per entry at a time: the setup
# backfill and the per-refresh extension both drive the same chain, and
# interleaved runs corrupt the cumulative sums (observed live as a
# mixed-baseline series). Keyed by entry so separate accounts don't skip
# each other's imports, while a reloaded entry still waits on its old run.
_import_locks: dict[str, asyncio.Lock] = {}


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
    # Energy is queued; never re-run it. A retry after the hourly upgrade
    # has started would put day rows back over its midnight hours and
    # break the chain.
    await coordinator.mark_stats_imported()
    try:
        await _async_import_cost_history(hass, entry, coordinator, history, today)
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "Cost history import failed; cost statistics start at setup",
            exc_info=True,
        )


async def _async_import_cost_history(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: NBPowerCoordinator,
    history: list[UsageRow],
    today: date,
) -> None:
    """Import cost history at day resolution and align its native block."""
    # Cost history: cycles and daily rows carry dollar amounts. The chain
    # ends at the books' own cumulative through the last imported day;
    # persist it so later alignment passes anchor there (not at the
    # sensor's current total, which also counts post-import books).
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
            cost_chain_end = cost_stats[-1]["sum"] or 0.0
            await coordinator.store_cost_chain_end(round(cost_chain_end, 2))
            _LOGGER.info(
                "Imported %s day-resolution cost statistics for %s",
                len(cost_points),
                cost_id,
            )
            # The cost entity's native rows (recorder-seeded) sit on a
            # zeroed baseline; lift them onto the imported chain end.
            await _async_align_native_block(
                hass,
                cost_id,
                cost_stats[-1]["start"],
                round(cost_chain_end, 2),
            )


async def async_backfill_hourly_statistics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: NBPowerCoordinator,
    *,
    wait: bool = False,
) -> None:
    """Import hourly statistics for published days, resumable and repeatable.

    Runs as a background task, so failures are logged here; the next
    refresh's pass resumes from the persisted progress. Both chains — the
    energy sensor's and the external statistics — run under one lock, so
    their portal requests never overlap. ``wait`` queues behind a running
    pass instead of skipping it.
    """
    lock = _import_locks.setdefault(entry.entry_id, asyncio.Lock())
    if lock.locked() and not wait:
        _LOGGER.debug("Statistics import already running; skipping")
        return
    async with lock:
        try:
            await _async_import_hourly(hass, entry, coordinator)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Hourly statistics import failed; it will retry on the next refresh",
                exc_info=True,
            )
        # Imported here: external_statistics builds on this module.
        from .external_statistics import async_update_external_statistics

        try:
            await async_update_external_statistics(hass, entry, coordinator)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Hourly external statistics update failed; it will retry on the "
                "next refresh",
                exc_info=True,
            )


async def _async_import_hourly(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: NBPowerCoordinator
) -> None:
    statistic_id = _energy_statistic_id(hass, entry)
    if statistic_id is None:
        return
    client = coordinator.client
    today = dt_util.now().date()
    window = (
        INTERVAL_BACKFILL_DAYS
        if entry.options.get(CONF_BACKFILL_HOURLY, True)
        else INTERVAL_RECENT_DAYS
    )
    start_day = today - timedelta(days=window)
    through = coordinator.stored_interval_through()
    # Resume exactly where the chain left off: jumping ahead to a newer
    # window start would skip days the stored seed does not include.
    resume_from = through + timedelta(days=1) if through else start_day
    # The recorder owns everything from the frontier onward; the import
    # never writes those hours (rewrites there get reverted and create
    # negative seams). On first completion the frontier is pinned to now.
    # The frontier DAY itself is half import-owned: its pre-frontier hours
    # predate the recorder's first row, so nothing else will ever write
    # them (a restart or statistics clear mid-day wipes the recorder's
    # coverage of those hours) — the import must fill them once published.
    frontier = coordinator.stored_stats_frontier()

    # Seed the hourly state series where the day-resolution series left
    # off: everything phase 1 recorded strictly before the first hour row.
    # A stored seed is only meaningful with its ``through`` (the two are
    # persisted together); until the first day completes, derive it for
    # this run's own starting day.
    seed = coordinator.stored_interval_state_seed()
    if seed is None or through is None:
        seed = 0.0
        if coordinator.history_rows:
            prior = [
                p for p in daily_points(coordinator.history_rows, today) if p[0] < resume_from
            ]
            seed = round(sum(kwh for _, kwh in prior), 3)

    imported = 0
    day = resume_from
    last_imported_start: datetime | None = None
    while True:
        frontier_day = frontier is not None and day == frontier.date()
        if frontier is not None and day > frontier.date():
            # Post-frontier days belong to the recorder entirely.
            break
        if day >= today and not frontier_day:
            break
        try:
            rows = await client.get_interval_usage(day)
        except (NBPowerError, TimeoutError, aiohttp.ClientError, OSError) as err:
            _LOGGER.warning(
                "Hourly statistics import stopped at %s (%s); it will retry "
                "on the next refresh",
                day,
                err,
            )
            return

        if rows:
            raw_points = hourly_points(rows)
            points = raw_points
            if frontier_day:
                # Keep only the import-owned hours: those that END at or
                # before the frontier. (The frontier can sit mid-hour; the
                # straddling hour's data stays split — its pre-frontier
                # quarter-hours are bounded, one-time loss.)
                points = [
                    (when, kwh)
                    for when, kwh in points
                    if dt_util.as_local(when) + timedelta(hours=1) <= frontier
                ]
            if points:
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

            if frontier_day:
                # Complete once every import-owned hour is in AND publication
                # has caught up past the frontier. The feed publishes in
                # blocks, not always in order (live: 00-07 and 16-23 present
                # while 08-15 were still missing), so data after the frontier
                # alone does not prove the owned hours are published.
                present = {when for when, _kwh in raw_points}
                owned = (datetime.combine(day, time(hour)) for hour in range(24))
                complete = all(
                    hour in present
                    for hour in owned
                    if dt_util.as_local(hour) + timedelta(hours=1) <= frontier
                ) and any(
                    dt_util.as_local(when) + timedelta(hours=1) > frontier
                    for when in present
                )
            else:
                complete = len(points) >= 24
            if not complete and (today - day).days > MI_SINCE_DAYS:
                # Still partial long after publication: a permanent gap
                # (meter outage). Keep what was published and move on rather
                # than stalling the import — and the frontier — on this day.
                complete = True
            if complete:
                # Complete day: advance the chain; the next run continues
                # from here.
                seed += sum(kwh for _, kwh in points)
            else:
                # The portal publishes days partially. This is the feed's
                # frontier: keep the persisted chain at the last complete
                # day (so this day re-fetches from the same base,
                # idempotently) and stop — later days cannot be trusted
                # to be complete either.
                _LOGGER.info(
                    "Day %s partially published (%s hours); will re-fetch",
                    day,
                    len(points),
                )
                break
        elif (today - day).days > MI_SINCE_DAYS:
            # Old day with nothing published; skip it permanently.
            pass
        else:
            # Recent day not published yet (the feed lags; unpublished days
            # come back empty). Do NOT mark it through — retried next pass.
            _LOGGER.info("Day %s not published yet; will retry", day)
            break

        await coordinator.store_interval_progress(day, seed)
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
        # Anchor the sensor at the value the recorder last saw, not at the
        # chain end: the recorder books every sensor step as consumption,
        # and the gap between the two (days the books had not caught up on)
        # is already in the imported chain. The sensor trails the chain by
        # that gap, as it does for below-frontier imports.
        published = coordinator.published_kwh()
        await coordinator.store_stats_frontier(
            dt_util.now(), published if published is not None else seed
        )
        _LOGGER.info("Statistics frontier pinned; recorder owns hours from here")
    # Align the recorder-owned blocks after the chain onto the chain ends
    # (a no-op once aligned; re-anchors the native block if it drifts —
    # the recorder's own baseline can restart at zero after restarts).
    through = coordinator.stored_interval_through()
    if through is not None:
        after = last_imported_start
        if after is None:
            after = dt_util.start_of_local_day(through) + timedelta(hours=23)
        await _async_align_native_block(hass, statistic_id, after, round(seed, 3))
        cost_id = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, UNIQUE_ID_TEMPLATE.format(entry_id=entry.entry_id, key=COST_KEY)
        )
        if cost_id and frontier is not None:
            # Anchor the cost alignment at the imported chain end, NOT the
            # sensor's current total: the recorder block legitimately sits
            # above the chain end by the deltas it captured since its
            # zero-point, and lifting it to the sensor total would count
            # the pre-import books gap twice. Entries that predate the
            # persisted anchor derive it once from the chain's last row
            # before the frontier, then keep it in storage.
            cost_chain_end = coordinator.stored_cost_chain_end()
            if cost_chain_end is None:
                cost_chain_end = await _async_derive_cost_chain_end(
                    hass, cost_id, frontier
                )
                if cost_chain_end is not None:
                    await coordinator.store_cost_chain_end(round(cost_chain_end, 3))
            if cost_chain_end is not None:
                await _async_align_native_block(
                    hass, cost_id, frontier, round(cost_chain_end, 2)
                )


def _row_ts(value: datetime | float) -> float:
    """Normalize a statistics row start (datetime or epoch) to epoch."""
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


async def _async_derive_cost_chain_end(
    hass: HomeAssistant, statistic_id: str, before: datetime
) -> float | None:
    """The imported cost chain's cumulative: its last row before ``before``."""
    from homeassistant.components.recorder.statistics import get_last_statistics

    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 72, statistic_id, False, {"sum"}
    )
    rows = sorted(
        (result.get(statistic_id) or []), key=lambda r: _row_ts(r["start"])
    )
    prior = [r for r in rows if _row_ts(r["start"]) <= before.timestamp()]
    if not prior:
        return None
    return round(prior[-1].get("sum") or 0.0, 3)


def _row_dt(value: datetime | float) -> datetime:
    """Normalize a statistics row start to an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else dt_util.utc_from_timestamp(value.timestamp())
    return dt_util.utc_from_timestamp(value)


async def _async_align_native_block(
    hass: HomeAssistant,
    statistic_id: str,
    after: datetime | None,
    chain_end: float | None,
) -> None:
    """Shift native recorder rows after the imported chain onto ``chain_end``.

    The recorder owns recent hours (its short-term data wins over row
    rewrites), so alignment goes through the recorder's own adjust job —
    queued on the recorder thread, so it serializes with the recorder's
    compiles — which updates long- AND short-term rows, keeping future
    recompiles consistent. Without this, the first native row after
    the imported chain sits at the books-fed sensor level, below the
    interval-fed chain, rendering as a negative bar at the seam.

    ``adjust_statistics`` shifts the target row AND every later row (both
    tables) by the same amount, so after each repair the cached sums of
    the remaining tail rows must be moved up by the same deficit —
    otherwise consecutive repairs compound off stale values and massively
    over-shift the tail.
    """
    if after is None:
        return
    from homeassistant.components.recorder.statistics import get_last_statistics

    # Raw statistic units: converted reads follow the entity's display unit
    # (e.g. MWh), which would be compared against a kWh chain end.
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 72, statistic_id, False, {"sum"}
    )
    all_rows = sorted(
        (result.get(statistic_id) or []), key=lambda r: _row_ts(r["start"])
    )
    tail = [r for r in all_rows if _row_ts(r["start"]) > after.timestamp()]
    if not tail:
        return

    # Anchor the running level at the imported chain end. A native block
    # sitting at/below it re-seeded from zero (the recorder restarts its
    # baseline after a statistics clear or a short-term purge); one lift
    # restores its internal deltas, which were always correct. When the
    # chain end is unknown (None), the last row at/before ``after`` — the
    # last imported row — anchors instead.
    if chain_end is not None:
        level = round(chain_end, 3)
    else:
        prior = [r for r in all_rows if _row_ts(r["start"]) <= after.timestamp()]
        level = round(prior[-1].get("sum") or 0.0, 3) if prior else 0.0
    for i, row in enumerate(tail):
        row_sum = round(row.get("sum") or 0.0, 3)
        deficit = round(level - row_sum, 3)
        if deficit > 0.01:
            get_instance(hass).async_adjust_statistics(
                statistic_id, _row_dt(row["start"]), deficit, "kWh"
            )
            _LOGGER.info(
                "Repaired native statistics dip for %s at %s (+%.3f)",
                statistic_id,
                row["start"],
                deficit,
            )
            # The adjust shifted every later row too; keep the cached tail
            # consistent so subsequent deficits are computed from reality.
            for later in tail[i + 1 :]:
                if later.get("sum") is not None:
                    later["sum"] = later["sum"] + deficit
        elif row_sum > level:
            level = row_sum
