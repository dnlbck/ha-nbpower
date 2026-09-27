"""DataUpdateCoordinator for the NB Power integration."""

from __future__ import annotations

import logging
from datetime import date, timedelta
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

    The counter is derived on every refresh from the portal's own books:

    * ``Mode=M`` returns ~36 completed billing cycles with kWh totals —
      the deep history, and the anchor of the counter.
    * ``Mode=D`` returns the trailing daily window (about one billing
      cycle, including the current cycle to date).

    Summing the monthly cycles plus the daily rows that fall after the last
    cycle's end date gives a consistent cumulative total with no persisted
    state, immune to meter re-validations inside completed cycles. (The
    window and cycles overlap on the last cycle's end date, which is why
    only strictly-later daily rows are added.)
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
        self._last_cumulative_kwh: float | None = None
        self._last_cumulative_cost: float | None = None

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
            raw = {}
            if floor_kwh is not None:
                raw["last_cumulative_kwh"] = floor_kwh
            if floor_cost is not None:
                raw["last_cumulative_cost"] = floor_cost
        self.stats_imported = bool(raw.get("stats_imported"))
        through = raw.get("interval_through")
        self._interval_through = date.fromisoformat(through) if through else None
        self._interval_state_seed = raw.get("interval_state_seed")
        self._last_cumulative_kwh = raw.get("last_cumulative_kwh")
        self._last_cumulative_cost = raw.get("last_cumulative_cost")

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

    async def raise_floor_to(self, value: float) -> None:
        """Raise the monotonic counter floor so the sensor reaches ``value``.

        Used after the hourly backfill: the interval-fed statistics chain
        can total more than the books-derived sensor, and the sensor must
        match its own history or the history/live seam renders negative.
        """
        if self._last_cumulative_kwh is None or value > self._last_cumulative_kwh:
            self._last_cumulative_kwh = round(value, 3)
            await self._async_save_stored()

    def stored_interval_state_seed(self) -> float | None:
        """Running total the hourly backfill's state series continues from."""
        return self._interval_state_seed

    async def store_interval_state_seed(self, seed: float) -> None:
        """Persist the hourly-backfill state seed (continues phase-1 series)."""
        self._interval_state_seed = seed
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
                "last_cumulative_kwh": self._last_cumulative_kwh,
                "last_cumulative_cost": self._last_cumulative_cost,
            }
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch usage data, logging in again if the widget token expired.

        Portal sessions are short-lived, so an expired token mid-refresh is
        routine: re-login transparently and retry once. Only a failed login
        (bad credentials) surfaces as ConfigEntryAuthFailed.
        """
        try:
            if not self.client.bootstrapped:
                await self.client.bootstrap(self._username, self._password)
            try:
                data = await self._async_fetch_all()
            except NBPowerAuthError:
                _LOGGER.debug("Widget token expired; logging in again")
                self.client.invalidate()
                await self.client.bootstrap(self._username, self._password)
                data = await self._async_fetch_all()
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
        self.hass.async_create_task(_run())

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
        daily_end = max(
            (row.start.date() for row in daily if row.kwh is not None), default=None
        )

        # The daily books lag several days and can even drop days that are
        # later re-booked at cycle close. The 15-minute feed publishes a
        # day's data only after that day ends, so only TODAY can be folded
        # into the cumulative here — older unpublished days are imported
        # with their real hours by the statistics extension instead.
        recent_days: dict[date, float] = {}
        today_kwh = 0.0
        try:
            rows = await self.client.get_interval_usage(today)
            if rows:
                today_kwh = round(sum(row.kwh or 0.0 for row in rows), 3)
                recent_days[today] = today_kwh
        except NBPowerApiError:
            _LOGGER.debug("No 15-minute data published for today yet")
        recent_kwh = today_kwh

        def _post_cycle(value) -> float:
            return sum(
                value(row) or 0.0
                for row in daily
                if row.kwh is not None
                and (last_cycle_end is None or row.start.date() > last_cycle_end)
            )

        books_kwh = round(sum(row.kwh or 0.0 for row in monthly) + _post_cycle(lambda r: r.kwh), 2)
        books_cost = round(
            sum(row.amount or 0.0 for row in monthly) + _post_cycle(lambda r: r.amount), 2
        )

        # Estimated days can be revised between refreshes; the Energy
        # dashboard treats a decreasing total_increasing sensor as a meter
        # reset (huge spikes). Hold the last value until the books catch
        # back up instead — under-counting slightly beats going backwards.
        derived_kwh = round(books_kwh + recent_kwh, 2)
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

        if not self.stats_imported and not self.history_rows:
            self.history_rows = monthly + [
                row
                for row in daily
                if row.kwh is not None
                and (last_cycle_end is None or row.start.date() > last_cycle_end)
            ]

        last_row = daily[-1] if daily else None
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
