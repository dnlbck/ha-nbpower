"""DataUpdateCoordinator for the NB Power integration."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import NBPowerClient, UsageRow
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EXTERNAL_GEN,
    MI_SINCE_DAYS,
    MIN_SCAN_INTERVAL,
    REPAIR_GEN,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .exceptions import NBPowerApiError, NBPowerAuthError

_LOGGER = logging.getLogger(__name__)

ATTR_DAILY_KWH = "daily_kwh"


class NBPowerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate NB Power portal data and the cumulative energy counter.

    The portal's books feed the cost and month-to-date sensors:

    * ``Mode=M`` returns ~36 completed billing cycles with kWh totals —
      the deep history.
    * ``Mode=D`` returns the trailing daily window (about one billing
      cycle, including the current cycle to date).

    Summing the monthly cycles plus the daily rows that fall after the last
    cycle's end date gives the books' cumulative total. (The window and
    cycles overlap on the last cycle's end date, which is why only
    strictly-later daily rows are added.)

    The energy counter uses the books only until the statistics frontier
    is pinned. From then on it runs on one ledger — the 15-minute interval
    feed, anchored at the value published when the frontier was pinned —
    because posting a billing cycle restructures the books by hundreds of
    kWh that the imported chain already counts.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: NBPowerClient,
        username: str,
        password: str,
        scan_interval: timedelta | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=max(
                scan_interval or DEFAULT_SCAN_INTERVAL, MIN_SCAN_INTERVAL
            ),
        )
        self.client = client
        self._username = username
        self._password = password
        self._stats_extension_running = False
        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        # Rows captured on the first refresh, consumed by the one-time
        # statistics backfill once the entities are registered.
        self.history_rows: list[UsageRow] = []
        self.stats_imported = False
        self._interval_through: date | None = None
        self._interval_state_seed: float | None = None
        self._stats_frontier: datetime | None = None
        self._frontier_end: float | None = None
        self._sensor_anchor_at: datetime | None = None
        self._sensor_anchor_kwh: float | None = None
        self._cost_chain_end: float | None = None
        self._last_cumulative_kwh: float | None = None
        self._last_cumulative_cost: float | None = None
        # Progress of the hourly external statistics; owned and interpreted
        # by external_statistics.py.
        self._external: dict[str, Any] = {}

    async def async_load_stored(self) -> None:
        """Load backfill progress and the monotonic-counter floor."""
        raw = await self._store.async_load() or {}
        if raw.get("stats_gen") != REPAIR_GEN:
            # Earlier builds wrote statistics with inconsistent state
            # chains (state series resetting at day or year boundaries).
            # Bumping REPAIR_GEN forces one clean re-import of everything
            # with the current chaining convention. The monotonic-counter
            # floor must SURVIVE the reset: without it, a books revision
            # on the first refresh after repair would let the sensor step
            # backwards (negative flow in the Energy dashboard).
            _LOGGER.info("Resetting statistics backfill for repair generation %s", REPAIR_GEN)
            floor_kwh = raw.get("last_cumulative_kwh")
            floor_cost = raw.get("last_cumulative_cost")
            # The external statistics are a separate chain with their own
            # generation; a sensor-chain repair must not rebuild them.
            external = raw.get("external")
            raw = {}
            if floor_kwh is not None:
                raw["last_cumulative_kwh"] = floor_kwh
            if floor_cost is not None:
                raw["last_cumulative_cost"] = floor_cost
            if external:
                raw["external"] = external
        self.stats_imported = bool(raw.get("stats_imported"))
        through = raw.get("interval_through")
        self._interval_through = date.fromisoformat(through) if through else None
        self._interval_state_seed = raw.get("interval_state_seed")
        self._stats_frontier = (
            datetime.fromisoformat(raw["stats_frontier"])
            if raw.get("stats_frontier")
            else None
        )
        self._frontier_end = raw.get("frontier_end")
        self._sensor_anchor_at = (
            datetime.fromisoformat(raw["sensor_anchor_at"])
            if raw.get("sensor_anchor_at")
            else None
        )
        self._sensor_anchor_kwh = raw.get("sensor_anchor_kwh")
        self._cost_chain_end = raw.get("cost_chain_end")
        self._last_cumulative_kwh = raw.get("last_cumulative_kwh")
        self._last_cumulative_cost = raw.get("last_cumulative_cost")
        self._external = dict(raw.get("external") or {})

    def external_state(self) -> dict[str, Any]:
        """Persisted progress of the hourly external statistics (a copy)."""
        return dict(self._external)

    async def async_store_external_state(self, state: dict[str, Any]) -> None:
        self._external = dict(state)
        await self._async_save_stored()

    @property
    def external_history_pending(self) -> bool:
        """True until the external statistics' day-resolution part is in."""
        return self._external.get("gen") != EXTERNAL_GEN or not self._external.get(
            "through"
        )

    async def mark_stats_imported(self) -> None:
        """Persist that the day-resolution statistics import completed."""
        self.stats_imported = True
        await self._async_save_stored()

    async def store_interval_through(self, through: date) -> None:
        """Persist hourly-backfill progress (resumable across restarts)."""
        self._interval_through = through
        await self._async_save_stored()

    def stored_interval_through(self) -> date | None:
        """Return the last day the hourly backfill completed, if any."""
        return self._interval_through

    def stored_interval_state_seed(self) -> float | None:
        """Running total the hourly backfill's state series continues from."""
        return self._interval_state_seed

    @property
    def _interval_seed(self) -> float | None:
        return self._interval_state_seed

    def stored_stats_frontier(self):
        """The datetime after which the recorder owns all statistics.

        The import must never write rows at or after this point: those
        hours belong to the recorder (its short-term data wins rewrites),
        and crossing the boundary is what creates negative seams.
        """
        return self._stats_frontier

    async def store_stats_frontier(self, frontier: datetime, anchor: float) -> None:
        self._stats_frontier = frontier
        self._frontier_end = round(anchor, 3)
        # A new frontier restarts the settled interval ledger.
        self._sensor_anchor_at = None
        self._sensor_anchor_kwh = None
        await self._async_save_stored()

    def stored_frontier_end(self) -> float | None:
        """Sensor value at the frontier — the interval ledger's anchor."""
        return self._frontier_end

    def published_kwh(self) -> float | None:
        """The energy total last published (what the recorder has seen)."""
        return self._last_cumulative_kwh

    async def store_frontier_end(self, value: float) -> None:
        self._frontier_end = round(value, 3)
        await self._async_save_stored()

    def stored_cost_chain_end(self) -> float | None:
        """Cumulative of the last imported cost-history row."""
        return self._cost_chain_end

    async def store_cost_chain_end(self, value: float) -> None:
        self._cost_chain_end = round(value, 3)
        await self._async_save_stored()

    async def store_interval_state_seed(self, seed: float) -> None:
        """Persist the hourly-backfill state seed (continues phase-1 series)."""
        self._interval_state_seed = seed
        await self._async_save_stored()

    async def store_interval_progress(self, through: date, seed: float) -> None:
        """Persist the hourly chain's resume day and its cumulative together.

        Saved apart, a stop between the two writes would re-import the day
        on top of a seed that already counts it — a phantom day of usage.
        """
        self._interval_through = through
        self._interval_state_seed = round(seed, 3)
        await self._async_save_stored()

    async def _async_save_stored(self) -> None:
        await self._store.async_save(
            {
                "stats_gen": REPAIR_GEN,
                "stats_imported": self.stats_imported,
                "interval_through": (
                    self._interval_through.isoformat()
                    if self._interval_through
                    else None
                ),
                "interval_state_seed": self._interval_state_seed,
                "stats_frontier": (
                    self._stats_frontier.isoformat()
                    if self._stats_frontier
                    else None
                ),
                "frontier_end": self._frontier_end,
                "sensor_anchor_at": (
                    self._sensor_anchor_at.isoformat()
                    if self._sensor_anchor_at
                    else None
                ),
                "sensor_anchor_kwh": self._sensor_anchor_kwh,
                "cost_chain_end": self._cost_chain_end,
                "last_cumulative_kwh": self._last_cumulative_kwh,
                "last_cumulative_cost": self._last_cumulative_cost,
                "external": self._external,
            }
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch usage data, logging in again if the widget token expired.

        Portal sessions are short-lived, so an expired token mid-refresh is
        routine: re-login transparently and retry once. Only a failed login
        (bad credentials) surfaces as ConfigEntryAuthFailed.
        """
        # The fetch exercises the token straight away, so the login skips
        # its own confirming usage request.
        try:
            if not self.client.bootstrapped:
                await self.client.bootstrap(
                    self._username, self._password, validate=False
                )
            try:
                data = await self._async_fetch_all()
            except NBPowerAuthError:
                _LOGGER.debug("Widget token expired; logging in again")
                self.client.invalidate()
                await self.client.bootstrap(
                    self._username, self._password, validate=False
                )
                try:
                    data = await self._async_fetch_all()
                except NBPowerAuthError as err:
                    # The login just succeeded, so the credentials are fine:
                    # retry next interval instead of demanding re-auth.
                    raise UpdateFailed(
                        f"NB Power rejected a fresh session: {err}"
                    ) from err
        except NBPowerAuthError as err:
            raise ConfigEntryAuthFailed(
                f"Could not sign in to NB Power: {err}"
            ) from err
        except NBPowerApiError as err:
            raise UpdateFailed(f"NB Power API error: {err}") from err
        except (TimeoutError, aiohttp.ClientError) as err:
            raise UpdateFailed(f"Error communicating with NB Power: {err}") from err

        self._async_schedule_stat_extension()
        return data

    def _async_schedule_stat_extension(self) -> None:
        """Import hourly statistics for newly published days after a refresh.

        The 15-minute feed publishes a day's data only after that day ends,
        so each refresh checks for newly available days (usually yesterday)
        and imports their real hourly usage.
        """
        if self._stats_extension_running:
            return
        entry = self.config_entry
        if entry is None:
            return

        async def _run() -> None:
            self._stats_extension_running = True
            try:
                from .statistics import async_backfill_hourly_statistics

                await async_backfill_hourly_statistics(self.hass, entry, self)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Hourly statistics extension failed", exc_info=True)
            finally:
                self._stats_extension_running = False

        self._stats_extension_running = True
        # Tied to the entry: cancelled on unload (a reloaded entry's old
        # coordinator must not keep writing the chain) and not awaited at
        # shutdown; the import resumes on the next refresh.
        entry.async_create_background_task(
            self.hass, _run(), f"{DOMAIN}_stats_extension_{entry.entry_id}"
        )

    async def _async_interval_ledger(
        self, frontier: datetime, frontier_end: float, today: date
    ) -> tuple[float, float]:
        """Return (sensor total, today's kWh) from the 15-minute feed.

        The total is the frontier anchor plus every interval published
        since the frontier. Days that have fully published fold into a
        persisted settled anchor, so a refresh only re-fetches the days
        still filling in — summing from the frontier on every refresh grew
        the request count daily, and capping that walk at MI_SINCE_DAYS
        dropped all post-frontier usage once the frontier aged past it.
        """
        settled = (
            self._sensor_anchor_at is not None and self._sensor_anchor_kwh is not None
        )
        anchor_at = self._sensor_anchor_at if settled else frontier
        total = self._sensor_anchor_kwh if settled else frontier_end
        settled_at, settled_kwh, settling = anchor_at, total, True
        today_kwh = 0.0
        day = anchor_at.date()
        while day <= today:
            rows = await self.client.get_interval_usage(day)
            day_kwh = sum(
                row.kwh or 0.0
                for row in rows
                if dt_util.as_local(row.start) >= anchor_at
            )
            total += day_kwh
            if day == today:
                today_kwh = day_kwh
            # Only contiguous days settle: fully published ones, or ones old
            # enough that a remaining gap is permanent (meter outage).
            published = len({row.start.hour for row in rows}) >= 24
            if (
                settling
                and day < today
                and (published or (today - day).days > MI_SINCE_DAYS)
            ):
                settled_at = dt_util.start_of_local_day(day + timedelta(days=1))
                settled_kwh = total
            else:
                settling = False
            day += timedelta(days=1)
        if settled_at != anchor_at:
            self._sensor_anchor_at = settled_at
            self._sensor_anchor_kwh = round(settled_kwh, 3)
            await self._async_save_stored()
        return round(total, 3), round(today_kwh, 3)

    async def _async_fetch_all(self) -> dict[str, Any]:
        now = dt_util.now()
        today = now.date()

        monthly, mtd = await self.client.get_monthly_usage()
        monthly = [row for row in monthly if row.kwh is not None]
        daily, daily_mtd = await self.client.get_daily_usage()

        # The monthly payload carries the real month-to-date block; the
        # daily payload's is a zeroed placeholder in live responses.
        tentative = mtd or daily_mtd

        last_cycle_end = max((row.end.date() for row in monthly if row.end), default=None)

        # ONE LEDGER for the energy sensor: the 15-minute interval feed,
        # anchored at the statistics frontier. The books-based cumulative
        # double counts when the portal restructures its ledger (posting a
        # billing cycle converts the daily window into a cycle total — a
        # jump of hundreds of kWh that the imported chain already
        # counted). Post-frontier intervals are not imported (the
        # recorder owns those hours), but they still drive the VALUE.
        frontier = self.stored_stats_frontier()
        recent_days: dict[date, float] = {}
        today_kwh = 0.0
        if frontier is not None and self.stored_frontier_end() is None:
            # Migration for entries pinned before frontier_end existed:
            # seed + the frontier day's pre-frontier intervals.
            try:
                day_rows = await self.client.get_interval_usage(frontier.date())
                # Interval rows carry naive local timestamps; the frontier
                # is aware — normalize before comparing.
                pre = sum(
                    row.kwh or 0.0
                    for row in day_rows
                    if dt_util.as_local(row.start) < frontier
                )
                await self.store_frontier_end(
                    (self._interval_seed or 0.0) + round(pre, 3)
                )
            except (NBPowerApiError, TimeoutError, aiohttp.ClientError):
                _LOGGER.debug("Could not migrate frontier end yet")
        frontier_end = self.stored_frontier_end()
        interval_total: float | None = None
        if frontier is not None and frontier_end is not None:
            # A flaky interval request must not fail the whole refresh (and
            # take every sensor unavailable): the books above already
            # arrived, and the energy sensor can hold for one interval.
            try:
                interval_total, today_kwh = await self._async_interval_ledger(
                    frontier, frontier_end, today
                )
            except (NBPowerApiError, TimeoutError, aiohttp.ClientError):
                _LOGGER.debug("Interval feed unavailable; sensor holds")
            if today_kwh:
                recent_days[today] = today_kwh

        def _post_cycle(value) -> float:
            return sum(
                value(row) or 0.0
                for row in daily
                if row.kwh is not None
                and (last_cycle_end is None or row.start.date() > last_cycle_end)
            )

        books_kwh = round(sum(row.kwh or 0.0 for row in monthly) + _post_cycle(lambda r: r.kwh), 2)
        if frontier is not None and frontier_end is not None:
            # An unavailable feed falls back to the anchor; the monotonic
            # floor below then holds the last value.
            books_kwh = round(
                interval_total if interval_total is not None else frontier_end, 2
            )
        books_cost = round(
            sum(row.amount or 0.0 for row in monthly) + _post_cycle(lambda r: r.amount), 2
        )

        # Estimated days can be revised between refreshes; the Energy
        # dashboard treats a decreasing total_increasing sensor as a meter
        # reset (huge spikes). Hold the last value until the books catch
        # back up instead — under-counting slightly beats going backwards.
        derived_kwh = books_kwh
        cumulative_kwh = (
            max(derived_kwh, self._last_cumulative_kwh)
            if self._last_cumulative_kwh is not None
            else derived_kwh
        )
        cumulative_cost = (
            max(books_cost, self._last_cumulative_cost)
            if self._last_cumulative_cost is not None
            else books_cost
        )
        if (
            cumulative_kwh != self._last_cumulative_kwh
            or cumulative_cost != self._last_cumulative_cost
        ):
            self._last_cumulative_kwh = cumulative_kwh
            self._last_cumulative_cost = cumulative_cost
            await self._async_save_stored()

        # History feeds the day-resolution imports and the hourly chain's
        # starting seed, so keep it until the hourly chain has started and
        # the external statistics have their day-resolution part.
        if (
            not self.stats_imported
            or self._interval_through is None
            or self.external_history_pending
        ) and not self.history_rows:
            self.history_rows = monthly + [
                row
                for row in daily
                if row.kwh is not None
                and (last_cycle_end is None or row.start.date() > last_cycle_end)
            ]

        # The newest window row can be a blank placeholder.
        last_row = next((row for row in reversed(daily) if row.kwh is not None), None)
        return {
            "cumulative_kwh": cumulative_kwh,
            "cumulative_cost": cumulative_cost,
            "last_daily_kwh": last_row.kwh if last_row else None,
            "last_daily_cost": last_row.amount if last_row else None,
            "last_daily_date": last_row.start.date() if last_row else None,
            "today_kwh": recent_days.get(today, 0.0),
            ATTR_DAILY_KWH: {
                row.start.date().isoformat(): row.kwh
                for row in daily
                if row.kwh is not None
            },
            "monthly_cycles": len(monthly),
            "tentative": tentative,
            "account_number": self.client.account_number,
            "meter_number": self.client.meter_number,
            "last_updated": now,
        }
