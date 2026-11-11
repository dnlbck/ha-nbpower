"""DataUpdateCoordinator for the NB Power integration."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
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
from .exceptions import NBPowerApiError, NBPowerAuthError, NBPowerSignInClosed
from .sign_in import ASK, GIVE_UP, SignInGuard

_LOGGER = logging.getLogger(__name__)

ATTR_DAILY_KWH = "daily_kwh"

# Stored values a statistics repair (REPAIR_GEN) must not reset: the
# counters' floors, the external statistics' own chain, the portal session
# and what the portal has made of recent sign-ins.
_KEPT_THROUGH_REPAIR = (
    "last_cumulative_kwh",
    "last_cumulative_cost",
    "external",
    "cost_ledger",
    "session",
    "sign_in",
)


def _store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, Any]]:
    return Store[dict[str, Any]](
        hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
    )


def _clock(when: datetime) -> str:
    """A moment as local wall-clock time, for messages."""
    return dt_util.as_local(when).strftime("%Y-%m-%d %H:%M")


def loaded_coordinator(entry: ConfigEntry) -> NBPowerCoordinator | None:
    """The entry's coordinator, while the entry is set up."""
    coordinator = getattr(entry, "runtime_data", None)
    if entry.state is ConfigEntryState.LOADED and isinstance(
        coordinator, NBPowerCoordinator
    ):
        return coordinator
    return None


async def async_stored_state(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """What an entry has stored, whether or not it is set up."""
    if coordinator := loaded_coordinator(entry):
        return coordinator.stored_state()
    return await _store(hass, entry).async_load() or {}


async def async_credentials_accepted(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Stop waiting out failed sign-ins: the portal just accepted credentials.

    For the config flow, after a re-authentication or reconfiguration signed
    in. The entry is reloaded next, and must not sit out a wait left by the
    refusals that came before.
    """
    if coordinator := loaded_coordinator(entry):
        await coordinator.async_sign_in_accepted()
        return
    store = _store(hass, entry)
    if (raw := await store.async_load()) and raw.get("sign_in"):
        guard = SignInGuard(raw["sign_in"])
        guard.succeeded(dt_util.utcnow(), "credentials entered again")
        await store.async_save({**raw, "sign_in": guard.as_dict()})


@callback
def async_hand_over_session(
    hass: HomeAssistant, unique_id: str, session: dict[str, Any] | None
) -> None:
    """Leave a sign-in for the next setup of the entry with this unique id.

    The config flow has just signed in to check the credentials; the setup
    that follows takes that session over instead of signing in a second
    time within seconds.
    """
    if session:
        hass.data.setdefault(DOMAIN, {})[unique_id] = session


async def async_remove_stored(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete a removed entry's stored progress."""
    await _store(hass, entry).async_remove()


class NBPowerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate NB Power portal data and the cumulative counters.

    The portal's books feed the month-to-date sensors, and both counters
    to begin with:

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

    The cost counter leaves the books too, for the hourly cost statistic
    (see :meth:`async_statistics_cost`): the portal only lists the last 36
    billing cycles, so the books' sum drops by a whole cycle each time a
    new one posts, and the counter, never allowed to decrease, stood still
    for weeks afterwards.

    Signing in is paced by a :class:`~.sign_in.SignInGuard`. The portal
    closes its login form for scheduled maintenance (seen live for 29
    hours; six outages in five weeks looked the same from here), and a
    sign-in that cannot be made must neither stop the integration until
    the password is typed in again, as it did up to 0.12, nor be retried
    at the pace Home Assistant retries a failed setup.
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
        self._scan_interval = max(
            scan_interval or DEFAULT_SCAN_INTERVAL, MIN_SCAN_INTERVAL
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=self._scan_interval,
        )
        self.client = client
        self._username = username
        self._password = password
        self._stats_extension_running = False
        self._store = _store(hass, entry)
        self._sign_in = SignInGuard()
        # True while the session in use came from storage or the config
        # flow and has not fetched anything yet.
        self._session_unproven = False
        # Where the cost counter stands relative to the statistics chain:
        # {"chain": <the chain's id>, "offset": <counter minus chain total>}.
        self._cost_ledger: dict[str, Any] | None = None
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
            # The external statistics are a separate chain with their own
            # generation; a sensor-chain repair must not rebuild them.
            raw = {
                key: raw[key]
                for key in _KEPT_THROUGH_REPAIR
                if raw.get(key) is not None
            }
        self._sign_in = SignInGuard(raw.get("sign_in"))
        # Go on with the sign-in the config flow just made, or the one in
        # use before the restart, rather than signing in again: a token that
        # has expired meanwhile shows on the first request, as always.
        handed_over = self.hass.data.get(DOMAIN, {}).pop(
            self.config_entry.unique_id, None
        )
        if session := handed_over or raw.get("session"):
            self.client.resume(session)
            self._session_unproven = True
        if handed_over and not self._sign_in.events:
            # A new entry: its first sign-in was the config flow's.
            self._sign_in.succeeded(dt_util.utcnow(), "signed in by the config flow")
        self._cost_ledger = dict(raw["cost_ledger"]) if raw.get("cost_ledger") else None
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

    async def async_statistics_cost(self, chain: str | None, total: float) -> None:
        """Advance the cost counter to the statistics chain's running total.

        Called after a pass that walked the hourly cost statistic through
        today, with the chain's id and its sum over every published hour.
        The counter moves by exactly what the chain gained since the last
        call. A chain met for the first time (after an upgrade, or one
        rebuilt from scratch) is taken up from the counter's present value,
        so changing ledgers never steps the sensor.
        """
        ledger = self._cost_ledger
        if ledger is None or ledger.get("chain") != chain:
            base = self._last_cumulative_cost
            ledger = {
                "chain": chain,
                "offset": round((total if base is None else base) - total, 3),
            }
        value = round(ledger["offset"] + total, 2)
        if self._last_cumulative_cost is not None:
            # Hours the portal revises downward must not run the counter back.
            value = max(value, self._last_cumulative_cost)
        if ledger != self._cost_ledger or value != self._last_cumulative_cost:
            self._cost_ledger = ledger
            self._last_cumulative_cost = value
            await self._async_save_stored()
        if self.data is not None and self.data.get("cumulative_cost") != value:
            self.data = {**self.data, "cumulative_cost": value}
            self.async_update_listeners()

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

    def stored_state(self) -> dict[str, Any]:
        """Everything the entry persists (also shown in the diagnostics)."""
        return {
            "stats_gen": REPAIR_GEN,
            "stats_imported": self.stats_imported,
            "interval_through": (
                self._interval_through.isoformat() if self._interval_through else None
            ),
            "interval_state_seed": self._interval_state_seed,
            "stats_frontier": (
                self._stats_frontier.isoformat() if self._stats_frontier else None
            ),
            "frontier_end": self._frontier_end,
            "sensor_anchor_at": (
                self._sensor_anchor_at.isoformat() if self._sensor_anchor_at else None
            ),
            "sensor_anchor_kwh": self._sensor_anchor_kwh,
            "cost_chain_end": self._cost_chain_end,
            "last_cumulative_kwh": self._last_cumulative_kwh,
            "last_cumulative_cost": self._last_cumulative_cost,
            "cost_ledger": self._cost_ledger,
            "external": self._external,
            "session": self.client.session_state(),
            "sign_in": self._sign_in.as_dict(),
        }

    async def _async_save_stored(self) -> None:
        await self._store.async_save(self.stored_state())

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch usage data, signing in first when no session will do.

        Portal sessions are short-lived, so a token that has expired since
        the last refresh is routine: sign in again and fetch.
        """
        # A sign-in that has to wait shortens this; anything else, the fetch
        # succeeding included, polls at the configured pace again.
        self.update_interval = self._scan_interval
        try:
            data: dict[str, Any] | None = None
            if self.client.bootstrapped:
                try:
                    data = await self._async_fetch_all()
                except NBPowerAuthError:
                    _LOGGER.debug("Widget token expired; signing in again")
                except NBPowerApiError:
                    # A session that was working is not to blame for this.
                    # One taken over at setup has yet to work at all, and a
                    # restart should not be stuck with it.
                    if not self._session_unproven:
                        raise
                if data is None:
                    # Out of storage too: a restart must not try it again.
                    self.client.invalidate()
                    await self._async_save_stored()
            if data is None:
                await self._async_sign_in()
                data = await self._async_fetch_fresh()
        except NBPowerApiError as err:
            raise UpdateFailed(f"NB Power API error: {err}") from err
        except (TimeoutError, aiohttp.ClientError) as err:
            raise UpdateFailed(f"Error communicating with NB Power: {err}") from err

        self._session_unproven = False
        self._async_schedule_stat_extension()
        return data

    async def _async_fetch_fresh(self) -> dict[str, Any]:
        """Fetch with the session a sign-in has just returned.

        If the portal has no data for it, there is still no second sign-in
        right away: not in this refresh, and not by a setup that is retried
        seconds from now. The next one waits as after a failed sign-in.
        """
        try:
            return await self._async_fetch_all()
        except (NBPowerAuthError, NBPowerApiError) as err:
            now = dt_util.utcnow()
            if rejected := isinstance(err, NBPowerAuthError):
                # The credentials are fine (the sign-in just succeeded), so
                # no re-auth; the token is no use, though.
                self.client.invalidate()
            self._sign_in.failed(now, f"No data after signing in: {err}")
            await self._async_save_stored()
            self._poll_at(self._sign_in.retry_at or now, now)
            raise UpdateFailed(
                f"NB Power rejected a fresh session: {err}"
                if rejected
                else f"NB Power API error: {err}"
            ) from err

    async def _async_sign_in(self) -> None:
        """Sign in to the portal, pacing the attempts after a failed one.

        The fetch exercises the token straight away, so the sign-in skips
        its own confirming usage request. A sign-in that cannot be made
        (the login form is off for maintenance) or that the portal turns
        away is waited out and retried (see sign_in.py): UpdateFailed while
        that goes on, ConfigEntryAuthFailed only once the portal has turned
        the credentials away for a week.
        """
        now = dt_util.utcnow()
        if (until := self._sign_in.wait_until(now)) is not None:
            self._poll_at(until, now)
            raise UpdateFailed(
                f"Waiting until {_clock(until)} to sign in to NB Power again"
                f" ({self._sign_in.last_failure or 'the last sign-in failed'})"
            )
        try:
            await self.client.bootstrap(self._username, self._password, validate=False)
        except NBPowerAuthError as err:
            verdict = self._sign_in.rejected(now, str(err))
            await self._async_save_stored()
            attempts = self._sign_in.rejections
            since = _clock(self._sign_in.rejected_since or now)
            if verdict == GIVE_UP:
                raise ConfigEntryAuthFailed(
                    f"{err} ({attempts} sign-ins turned away since {since})"
                ) from err
            until = self._sign_in.retry_at or now
            self._poll_at(until, now)
            if verdict == ASK:
                # A day of refusals: time to ask whether the password
                # changed. The portal may still come round by itself, so
                # keep trying (the request is withdrawn if it does).
                self.config_entry.async_start_reauth(self.hass)
            _LOGGER.warning(
                "%s. Sign-in %s turned away since %s; next attempt at %s%s",
                err,
                attempts,
                since,
                _clock(until),
                " (re-authentication requested meanwhile)" if verdict == ASK else "",
            )
            raise UpdateFailed(f"{err}; next attempt at {_clock(until)}") from err
        except NBPowerApiError as err:
            # The login form switched off for maintenance (the usual case),
            # a 5xx, a page that is not the portal's: no verdict on the
            # credentials, and worth looking again sooner than a refusal.
            self._sign_in.failed(now, str(err))
            await self._async_save_stored()
            self._poll_at(self._sign_in.retry_at or now, now)
            raise UpdateFailed(
                str(err)
                if isinstance(err, NBPowerSignInClosed)
                else f"NB Power sign-in failed: {err}"
            ) from err
        self._session_unproven = False
        lasted = self._sign_in.succeeded(now)
        await self._async_save_stored()
        if lasted is not None:
            _LOGGER.info(
                "NB Power accepts the sign-in again, after turning it away for %s",
                str(lasted).split(".")[0],
            )
            self._async_withdraw_reauth()

    def _poll_at(self, when: datetime, now: datetime) -> None:
        """Refresh when a sign-in wait ends, if that is before the next poll."""
        wait = max(when - now, timedelta(seconds=30)) + timedelta(seconds=5)
        self.update_interval = min(self._scan_interval, wait)

    def _async_withdraw_reauth(self) -> None:
        """Drop a pending request for credentials that turned out to work."""
        entry = self.config_entry
        for flow in list(entry.async_get_active_flows(self.hass, {SOURCE_REAUTH})):
            self.hass.config_entries.flow.async_abort(flow["flow_id"])

    async def async_sign_in_accepted(self) -> None:
        """Credentials the user entered again were accepted (config flow)."""
        self._sign_in.succeeded(dt_util.utcnow(), "credentials entered again")
        await self._async_save_stored()

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
        if self._cost_ledger is not None and self._last_cumulative_cost is not None:
            # The cost counter rides the statistics chain: the pass that
            # follows this refresh advances it (async_statistics_cost).
            books_cost = self._last_cumulative_cost

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
