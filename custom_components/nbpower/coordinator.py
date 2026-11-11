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
    MIN_SCAN_INTERVAL,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .exceptions import NBPowerApiError, NBPowerAuthError, NBPowerSignInClosed
from .sign_in import ASK, GIVE_UP, SignInGuard

_LOGGER = logging.getLogger(__name__)

ATTR_DAILY_KWH = "daily_kwh"

# A ledger that follows no chain yet: its counter stands still until a
# statistics pass takes it up from there.
_NO_CHAIN = ""

# What 0.13 and earlier stored for the statistics they backfilled under the
# energy sensor's own id and for the 15-minute ledger behind that sensor.
# Nothing here uses them beyond _ledger_from_interval_feed; they are stored
# back as found, so that going back to 0.13 carries on where it left off
# instead of importing everything again.
_STORED_BY_0_13 = (
    "stats_gen",
    "stats_imported",
    "interval_through",
    "interval_state_seed",
    "stats_frontier",
    "frontier_end",
    "sensor_anchor_at",
    "sensor_anchor_kwh",
    "cost_chain_end",
)


def _store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, Any]]:
    return Store[dict[str, Any]](
        hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
    )


def _clock(when: datetime) -> str:
    """A moment as local wall-clock time, for messages."""
    return dt_util.as_local(when).strftime("%Y-%m-%d %H:%M")


def _ledger_from_interval_feed(
    stored: dict[str, Any], external: dict[str, Any]
) -> dict[str, Any]:
    """Place the energy counter in the statistics chain, coming from 0.13.

    There the counter ran on the 15-minute feed. ``sensor_anchor_kwh`` was
    its value with every day before ``sensor_anchor_at`` (a local midnight)
    counted in full, and the chain has its own figure for the same days:
    ``energy_sum`` through the day ``through``. Where the two stop at the
    same midnight, their difference is the counter's offset from the chain,
    and the counter comes out where the old ledger would stand today —
    whatever was published while the entry was down or being upgraded
    included. Otherwise the counter is held where it stands until a pass
    takes it up, and what was published in between goes uncounted.
    """
    try:
        anchor = datetime.fromisoformat(stored["sensor_anchor_at"])
        through = date.fromisoformat(external["through"])
        offset = round(float(stored["sensor_anchor_kwh"]) - external["energy_sum"], 3)
    except (KeyError, TypeError, ValueError):
        return {"chain": _NO_CHAIN, "offset": 0.0}
    if (
        external.get("gen") != EXTERNAL_GEN
        or anchor.time() != datetime.min.time()
        or anchor.date() != through + timedelta(days=1)
    ):
        return {"chain": _NO_CHAIN, "offset": 0.0}
    return {"chain": external.get("chain"), "offset": offset}


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

    Each refresh fetches the portal's books:

    * ``Mode=M`` returns ~36 completed billing cycles with kWh totals —
      the deep history.
    * ``Mode=D`` returns the trailing daily window (about one billing
      cycle, including the current cycle to date).

    They feed the month-to-date sensors and the day-resolution start of
    the statistics, and their sum (the cycles plus the daily rows after
    the last cycle's end date; the two overlap on that date) is where the
    two cumulative counters begin.

    The counters do not stay on the books: posting a billing cycle
    restructures them by hundreds of kWh, and only the last 36 cycles are
    listed, so the sum drops by a whole cycle whenever a new one posts.
    Once the statistics have been written through today, each counter
    follows its statistic's running total instead (see
    :meth:`async_statistics_totals`): one ledger for the dashboard and the
    sensors, fed by the hourly feed.

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
        self._statistics_running = False
        self._store = _store(hass, entry)
        self._sign_in = SignInGuard()
        # True while the session in use came from storage or the config
        # flow and has not fetched anything yet.
        self._session_unproven = False
        # The books' rows from the first refresh: the day-resolution start
        # of the statistics, until that part has been written.
        self.history_rows: list[UsageRow] = []
        # The counters as last published (they never decrease), and where
        # each stands relative to the statistics chain it follows:
        # {"chain": <the chain's id>, "offset": <counter minus chain total>}.
        # None while a counter is still on the books.
        self._last_cumulative_kwh: float | None = None
        self._last_cumulative_cost: float | None = None
        self._energy_ledger: dict[str, Any] | None = None
        self._cost_ledger: dict[str, Any] | None = None
        # Progress of the statistics; owned and interpreted by
        # external_statistics.py.
        self._external: dict[str, Any] = {}
        self._stored_by_0_13: dict[str, Any] = {}

    async def async_load_stored(self) -> None:
        """Load the counters, the statistics' progress and the session."""
        raw = await self._store.async_load() or {}
        self._stored_by_0_13 = {
            key: raw[key] for key in _STORED_BY_0_13 if key in raw
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
        self._last_cumulative_kwh = raw.get("last_cumulative_kwh")
        self._last_cumulative_cost = raw.get("last_cumulative_cost")
        self._external = dict(raw.get("external") or {})
        self._cost_ledger = dict(raw["cost_ledger"]) if raw.get("cost_ledger") else None
        if raw.get("energy_ledger"):
            self._energy_ledger = dict(raw["energy_ledger"])
        elif raw.get("stats_frontier"):
            # Stored by 0.13 or earlier, with the energy counter already
            # off the books (on the 15-minute feed, since retired).
            self._energy_ledger = _ledger_from_interval_feed(raw, self._external)

    def external_state(self) -> dict[str, Any]:
        """Persisted progress of the statistics (a copy)."""
        return dict(self._external)

    async def async_store_external_state(self, state: dict[str, Any]) -> None:
        self._external = dict(state)
        await self._async_save_stored()

    @property
    def external_history_pending(self) -> bool:
        """True until the statistics' day-resolution part is in."""
        return self._external.get("gen") != EXTERNAL_GEN or not self._external.get(
            "through"
        )

    async def async_statistics_totals(
        self, chain: str | None, energy: float, cost: float
    ) -> None:
        """Advance the counters to the statistics chains' running totals.

        Called after a pass that walked the statistics through today, with
        the chain's id and each statistic's sum over every published hour.
        A counter moves by exactly what its chain gained since the last
        call. A chain met for the first time (on a new entry, after an
        upgrade, or rebuilt from scratch) is taken up from the counter's
        present value, so changing ledgers never steps a sensor.
        """
        energy_ledger, kwh = self._followed(
            self._energy_ledger, chain, energy, self._last_cumulative_kwh, 3
        )
        cost_ledger, dollars = self._followed(
            self._cost_ledger, chain, cost, self._last_cumulative_cost, 2
        )
        stored = (
            self._energy_ledger,
            self._cost_ledger,
            self._last_cumulative_kwh,
            self._last_cumulative_cost,
        )
        if (energy_ledger, cost_ledger, kwh, dollars) != stored:
            self._energy_ledger, self._cost_ledger = energy_ledger, cost_ledger
            self._last_cumulative_kwh, self._last_cumulative_cost = kwh, dollars
            await self._async_save_stored()
        shown = self.data or {}
        if self.data is not None and (
            shown.get("cumulative_kwh"),
            shown.get("cumulative_cost"),
        ) != (kwh, dollars):
            self.data = {**self.data, "cumulative_kwh": kwh, "cumulative_cost": dollars}
            self.async_update_listeners()

    @staticmethod
    def _followed(
        ledger: dict[str, Any] | None,
        chain: str | None,
        total: float,
        last: float | None,
        digits: int,
    ) -> tuple[dict[str, Any], float]:
        """A counter's ledger and value, with its chain totalling ``total``."""
        if ledger is None or ledger.get("chain") != chain:
            ledger = {
                "chain": chain,
                "offset": round((total if last is None else last) - total, 3),
            }
        value = round(ledger["offset"] + total, digits)
        if last is not None:
            # Hours the portal revises downward must not run the counter
            # back: a total_increasing sensor that decreases reads as a
            # meter reset.
            value = max(value, last)
        return ledger, value

    @staticmethod
    def _held(ledger: dict[str, Any] | None, last: float | None, books: float) -> float:
        """A counter's value at a refresh, which only reads the books.

        Following the statistics, the counter stands where the last pass
        left it. Before that it is the books' sum, held at its last value
        whenever that comes down (estimated days get revised).
        """
        if last is None:
            return books
        if ledger is not None:
            return last
        return max(books, last)

    def stored_state(self) -> dict[str, Any]:
        """Everything the entry persists (also shown in the diagnostics)."""
        return {
            **self._stored_by_0_13,
            "last_cumulative_kwh": self._last_cumulative_kwh,
            "last_cumulative_cost": self._last_cumulative_cost,
            "energy_ledger": self._energy_ledger,
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
        self._async_schedule_statistics()
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

    def _async_schedule_statistics(self) -> None:
        """Bring the statistics up to date after a refresh, in the background.

        The portal publishes a day's usage a day or more after it ends, in
        blocks, so each refresh is followed by a pass over the days still
        filling in (usually one to three requests).
        """
        if self._statistics_running:
            return
        entry = self.config_entry

        async def _run() -> None:
            try:
                # Imported here: external_statistics builds on this module.
                from .external_statistics import async_update_statistics

                await async_update_statistics(self.hass, entry, self)
            finally:
                self._statistics_running = False

        self._statistics_running = True
        # Tied to the entry: cancelled on unload (a reloaded entry's old
        # coordinator must not keep writing the chain) and not awaited at
        # shutdown; the next refresh's pass resumes from the stored progress.
        entry.async_create_background_task(
            self.hass, _run(), f"{DOMAIN}_statistics_{entry.entry_id}"
        )

    async def _async_fetch_all(self) -> dict[str, Any]:
        now = dt_util.now()

        monthly, mtd = await self.client.get_monthly_usage()
        monthly = [row for row in monthly if row.kwh is not None]
        daily, daily_mtd = await self.client.get_daily_usage()

        # The monthly payload carries the real month-to-date block; the
        # daily payload's is a zeroed placeholder in live responses.
        tentative = mtd or daily_mtd

        # The books: every cycle, then the daily rows after the last one.
        last_cycle_end = max((row.end.date() for row in monthly if row.end), default=None)
        books = monthly + [
            row
            for row in daily
            if row.kwh is not None
            and (last_cycle_end is None or row.start.date() > last_cycle_end)
        ]
        cumulative_kwh = self._held(
            self._energy_ledger,
            self._last_cumulative_kwh,
            round(sum(row.kwh or 0.0 for row in books), 2),
        )
        cumulative_cost = self._held(
            self._cost_ledger,
            self._last_cumulative_cost,
            round(sum(row.amount or 0.0 for row in books), 2),
        )
        if (
            cumulative_kwh != self._last_cumulative_kwh
            or cumulative_cost != self._last_cumulative_cost
        ):
            self._last_cumulative_kwh = cumulative_kwh
            self._last_cumulative_cost = cumulative_cost
            await self._async_save_stored()

        if self.external_history_pending and not self.history_rows:
            self.history_rows = books

        # The newest window row can be a blank placeholder.
        last_row = next((row for row in reversed(daily) if row.kwh is not None), None)
        return {
            "cumulative_kwh": cumulative_kwh,
            "cumulative_cost": cumulative_cost,
            "last_daily_kwh": last_row.kwh if last_row else None,
            "last_daily_cost": last_row.amount if last_row else None,
            "last_daily_date": last_row.start.date() if last_row else None,
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
