"""The hourly statistics the Energy dashboard reads, and the two cumulative
sensors that follow them."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from functools import partial
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import conftest as nb
from custom_components.nbpower import external_statistics as nb_ext
from custom_components.nbpower.api import UsageRow
from custom_components.nbpower.const import DOMAIN, EXTERNAL_GEN
from custom_components.nbpower.coordinator import _ledger_from_interval_feed
from custom_components.nbpower.external_statistics import hour_buckets, statistic_ids

FLOW_INPUT = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "s3cret"}


async def _setup(hass, portal, nbpower_urls, *, backfill_hourly=True):
    nbpower_urls(nb.server_base(portal))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"NB Power {nb.ACCOUNT_NUMBER}",
        data=FLOW_INPUT,
        options=None if backfill_hourly else {"backfill_hourly": False},
        unique_id=f"account_{nb.ACCOUNT_NUMBER}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await nb.recorder_done(hass)
    return entry


async def _refresh(hass, coordinator):
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await nb.recorder_done(hass)


async def _rows(hass, statistic_id):
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utc_from_timestamp(0),
        dt_util.now() + timedelta(days=2), {statistic_id}, "hour", None, {"sum"},
    )
    return sorted(stats.get(statistic_id, []), key=lambda r: r["start"])


def _sensor_value(hass, entry, key="energy_usage") -> float:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{key}"
    )
    return float(hass.states.get(entity_id).state)


def _counters(hass, entry) -> tuple[float, float]:
    """The cumulative energy and cost sensors."""
    return _sensor_value(hass, entry), _sensor_value(hass, entry, "cost_usage")


def _local(row) -> datetime:
    return dt_util.as_local(dt_util.utc_from_timestamp(row["start"]))


def _changes(rows):
    """(local start, change) per row, change from the previous row's sum."""
    return [
        (_local(row), row["sum"] - prev["sum"]) for prev, row in zip(rows, rows[1:])
    ]


def _portal_hours(day: date) -> list[tuple[datetime, float, float]]:
    """The mock portal's hourly (start, kWh, dollars) for a day, merged by
    instant like a DST day needs — worked out here, not by the client."""
    hours: dict[datetime, tuple[float, float]] = {}
    for row in nb._hourly_rows(day):
        naive = datetime.combine(day, time(int(row["Hourly"][:2])))
        start = dt_util.as_local(dt_util.utc_from_timestamp(dt_util.as_local(naive).timestamp()))
        kwh, cost = hours.get(start, (0.0, 0.0))
        hours[start] = (kwh + row["Consumption"], cost + row["Amount"])
    return [(start, kwh, cost) for start, (kwh, cost) in sorted(hours.items())]


def _day_kwh(day: date) -> float:
    return sum(kwh for _, kwh, _ in _portal_hours(day))


def _held_back(day: date) -> tuple[float, float]:
    """(kWh, dollars) of hours 08-15, which the mock portal publishes last
    for the day before yesterday (its ``partial_day`` state)."""
    hours = [(kwh, cost) for start, kwh, cost in _portal_hours(day) if 8 <= start.hour <= 15]
    return sum(kwh for kwh, _ in hours), sum(cost for _, cost in hours)


def _assert_hours_match_portal(rows, first_day: date, last_day: date, index: int):
    """Each hour from first_day..last_day carries the portal's value."""
    changes = dict(_changes(rows))
    day = first_day
    while day <= last_day:
        expected = _portal_hours(day)
        assert expected
        for start, *values in expected:
            assert changes[start] == pytest.approx(values[index], abs=0.002), start
        day += timedelta(days=1)


async def test_hourly_statistics_follow_the_portal(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations
):
    entry = await _setup(hass, portal, nbpower_urls)
    energy_id, cost_id = statistic_ids(entry, entry.runtime_data)
    state = entry.runtime_data.external_state()
    window_start = date.fromisoformat(state["window_start"])
    through = date.fromisoformat(state["through"])
    assert through == date.today() - timedelta(days=1)

    energy = await _rows(hass, energy_id)
    cost = await _rows(hass, cost_id)
    # Every hour of the window holds its own usage and cost...
    _assert_hours_match_portal(energy, window_start, through, 0)
    _assert_hours_match_portal(cost, window_start, through, 1)
    # ...after the day-resolution history, and the chain never dips.
    assert _local(energy[0]).date() < window_start
    assert min(change for _, change in _changes(energy)) >= 0
    assert energy[-1]["sum"] == pytest.approx(state["energy_sum"], abs=0.001)
    assert cost[-1]["sum"] == pytest.approx(state["cost_sum"], abs=0.001)


async def test_out_of_order_block_lands_in_its_hours(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations
):
    """Live: a day published hours 00-07 and 16-23 while 08-15 were missing."""
    partial = date.today() - timedelta(days=2)
    portal.app["state"]["partial_day"] = True
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    energy_id, _ = statistic_ids(entry, coord)
    # The chain stays final through the day before the hole...
    assert coord.external_state()["through"] == (partial - timedelta(days=1)).isoformat()
    rows = await _rows(hass, energy_id)
    hours = sorted(_local(r).hour for r in rows if _local(r).date() == partial)
    assert hours == [*range(8), *range(16, 24)]
    # ...yet yesterday is already in, on top of what was published.
    assert {_local(r).date() for r in rows} >= {partial + timedelta(days=1)}
    before = rows[-1]["sum"]

    portal.app["state"]["partial_day"] = False
    await _refresh(hass, coord)
    rows = await _rows(hass, energy_id)
    assert coord.external_state()["through"] == (date.today() - timedelta(days=1)).isoformat()
    _assert_hours_match_portal(rows, partial, partial + timedelta(days=1), 0)
    assert min(change for _, change in _changes(rows)) >= 0
    block = sum(kwh for start, kwh, _ in _portal_hours(partial) if 8 <= start.hour <= 15)
    assert rows[-1]["sum"] - before == pytest.approx(block, abs=0.01)


async def test_old_gap_does_not_stall_the_statistics(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations,
    monkeypatch,
):
    """An hour the meter never reported stays missing for good. Days that
    old are taken as they are, or everything after them would wait."""
    monkeypatch.setattr(nb_ext, "HOURLY_DAYS", 15)
    outage = date.today() - timedelta(days=13)
    hourly = nb._hourly_rows
    monkeypatch.setattr(
        nb, "_hourly_rows",
        lambda day: [r for r in hourly(day) if not (day == outage and r["Hourly"] == "02:00")],
    )
    entry = await _setup(hass, portal, nbpower_urls)
    energy_id, _ = statistic_ids(entry, entry.runtime_data)
    assert entry.runtime_data.external_state()["through"] == (
        date.today() - timedelta(days=1)
    ).isoformat()
    rows = await _rows(hass, energy_id)
    assert min(change for _, change in _changes(rows)) >= 0
    assert len([r for r in rows if _local(r).date() == outage]) == 23


async def test_backfill_option_off_keeps_the_recent_days_hourly(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations,
    monkeypatch,
):
    """Off means no year of hourly history. The days the portal is still
    publishing are hourly either way, so late blocks land in their hours."""
    monkeypatch.setattr(nb_ext, "HOURLY_DAYS", 15)
    monkeypatch.setattr(nb_ext, "HOURLY_DAYS_WITHOUT_BACKFILL", 5)
    entry = await _setup(hass, portal, nbpower_urls, backfill_hourly=False)
    energy_id, _ = statistic_ids(entry, entry.runtime_data)
    rows = await _rows(hass, energy_id)
    hourly_days = sorted({_local(r).date() for r in rows if _local(r).hour})
    assert hourly_days[0] == date.today() - timedelta(days=5)
    assert hourly_days[-1] == date.today() - timedelta(days=1)
    # Before that, the books at one row a day.
    assert _local(rows[0]).date() < date.today() - timedelta(days=700)


async def test_statistics_updates_queue_per_entry(hass, monkeypatch):
    """One entry's updates run one after the other; another account's does
    not wait for them."""
    entry_a = MockConfigEntry(domain=DOMAIN, unique_id="account_a")
    entry_b = MockConfigEntry(domain=DOMAIN, unique_id="account_b")
    calls = []

    async def _probe(hass, entry, coordinator):
        calls.append(entry.entry_id)

    monkeypatch.setattr(nb_ext, "_async_update", _probe)
    lock_a = nb_ext._locks.setdefault(entry_a.entry_id, asyncio.Lock())
    async with lock_a:  # an update of A is under way
        queued = asyncio.ensure_future(nb_ext.async_update_statistics(hass, entry_a, None))
        await nb_ext.async_update_statistics(hass, entry_b, None)
        await asyncio.sleep(0)
        assert calls == [entry_b.entry_id]
    await queued
    assert calls == [entry_b.entry_id, entry_a.entry_id]


async def test_rebuilt_statistics_do_not_step_the_sensors(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations
):
    """A chain that is started over totals differently (other window, other
    rounding); the sensors take it up from where they stand."""
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    energy_id, cost_id = statistic_ids(entry, coord)
    first_chain = coord.external_state()["chain"]
    await coord.async_store_external_state({})
    coord.history_rows = []
    get_instance(hass).async_clear_statistics([energy_id, cost_id])
    await nb.recorder_done(hass)
    before = _counters(hass, entry)

    await _refresh(hass, coord)
    state = coord.external_state()
    assert state["through"] == (date.today() - timedelta(days=1)).isoformat()
    rows = await _rows(hass, energy_id)
    assert _local(rows[0]).date() < date.fromisoformat(state["window_start"])
    assert state["chain"] != first_chain
    stored = coord.stored_state()
    assert stored["energy_ledger"]["chain"] == state["chain"]
    assert stored["cost_ledger"]["chain"] == state["chain"]
    assert _counters(hass, entry) == pytest.approx(before, abs=0.011)


async def test_counters_follow_the_statistics_not_the_books(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations,
    monkeypatch,
):
    """Live: the portal lists only the last 36 billing cycles. Each time a
    new one posts the oldest drops out and the books' sum falls by a whole
    cycle, so the cost sensor, held at its last value, stood still for
    weeks at a time. The statistics have no such steps."""
    held_back = date.today() - timedelta(days=2)
    portal.app["state"]["partial_day"] = True
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    energy, cost = _counters(hass, entry)

    # The oldest cycle drops out of the books (they total less than before)
    # while the portal publishes the hours it was holding back.
    cycles = nb._monthly_cycles
    monkeypatch.setattr(nb, "_monthly_cycles", lambda: cycles(35))
    portal.app["state"]["partial_day"] = False
    await _refresh(hass, coord)

    kwh, dollars = _held_back(held_back)
    assert cycles(36)[-1]["Consumption"] > 1000 > kwh > 5
    assert cycles(36)[-1]["Amount"] > 100 > dollars > 0.5
    assert _counters(hass, entry) == pytest.approx((energy + kwh, cost + dollars), abs=0.011)


async def test_counters_wait_for_a_pass_that_reaches_today(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations,
    monkeypatch,
):
    """A pass cut short has not counted every published hour; the totals it
    got to say nothing about where the sensors should stand."""
    held_back = date.today() - timedelta(days=2)
    portal.app["state"]["partial_day"] = True
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    energy, cost = _counters(hass, entry)
    hourly = coord.client.get_hourly_usage

    async def _fails_on_yesterday(day):
        if day == date.today() - timedelta(days=1):
            raise aiohttp.ClientConnectionError("connection reset")
        return await hourly(day)

    monkeypatch.setattr(coord.client, "get_hourly_usage", _fails_on_yesterday)
    portal.app["state"]["partial_day"] = False
    await _refresh(hass, coord)
    # The books arrived, so the refresh is fine and the sensors stay up.
    assert coord.last_update_success
    assert _counters(hass, entry) == (energy, cost)

    monkeypatch.setattr(coord.client, "get_hourly_usage", hourly)
    await _refresh(hass, coord)
    kwh, dollars = _held_back(held_back)
    assert _counters(hass, entry) == pytest.approx((energy + kwh, cost + dollars), abs=0.011)


async def test_counters_hold_when_the_portal_takes_hours_back(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations,
    monkeypatch,
):
    """A total_increasing sensor that decreases reads as a meter reset. If
    a day still being published comes back with less than before, the
    sensors wait for the statistics to pass their mark again."""
    held_back = date.today() - timedelta(days=2)
    portal.app["state"]["partial_day"] = True  # hours 00-07 and 16-23 are in
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    energy, cost = _counters(hass, entry)

    hourly = nb._hourly_rows
    monkeypatch.setattr(
        nb, "_hourly_rows",
        lambda day: [r for r in hourly(day) if not (day == held_back and r["Hourly"] >= "16:00")],
    )
    await _refresh(hass, coord)
    assert _counters(hass, entry) == (energy, cost)

    monkeypatch.setattr(nb, "_hourly_rows", hourly)
    portal.app["state"]["partial_day"] = False
    await _refresh(hass, coord)
    kwh, dollars = _held_back(held_back)
    assert _counters(hass, entry) == pytest.approx((energy + kwh, cost + dollars), abs=0.011)


def _as_stored_by_0_13(stored: dict, *, anchor_at: datetime | None, anchor_kwh: float | None):
    """Turn an entry's storage into what 0.13 left behind: no ledger for the
    energy sensor, but the statistics frontier of the import it ran under
    that sensor's own id and the 15-minute ledger's settled anchor."""
    del stored["energy_ledger"]
    stored.update(
        stats_gen=9,
        stats_imported=True,
        interval_through=(date.today() - timedelta(days=40)).isoformat(),
        interval_state_seed=79000.0,
        stats_frontier=(dt_util.now() - timedelta(days=40)).isoformat(),
        frontier_end=79000.0,
        sensor_anchor_at=anchor_at.isoformat() if anchor_at else None,
        sensor_anchor_kwh=anchor_kwh,
        cost_chain_end=9000.0,
    )
    return {key: stored[key] for key in (
        "stats_gen", "stats_imported", "interval_through", "interval_state_seed",
        "stats_frontier", "frontier_end", "sensor_anchor_at", "sensor_anchor_kwh",
        "cost_chain_end",
    )}


async def test_upgrade_from_the_15_minute_ledger_carries_on_exactly(
    recorder_mock, hass, hass_storage, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    """Up to 0.13 the energy sensor ran on the 15-minute feed, anchored at
    its value with every day before some midnight counted in full. The
    statistics count the same usage: from the same midnight on, the sensor
    stands where that ledger would — including what the portal published
    while the entry was down."""
    held_back = date.today() - timedelta(days=2)
    yesterday = date.today() - timedelta(days=1)
    portal.app["state"]["partial_day"] = True
    entry = await _setup(hass, portal, nbpower_urls)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[f"nbpower.{entry.entry_id}"]["data"]
    assert stored["external"]["through"] == (held_back - timedelta(days=1)).isoformat()
    # As 0.13 left it: settled through the day before the hole, like the
    # statistics, and of what came after it had only seen that day's 16
    # hours when it last refreshed. Yesterday was not published yet.
    seen = _day_kwh(held_back) - _held_back(held_back)[0]
    left_by_0_13 = _as_stored_by_0_13(
        stored, anchor_at=dt_util.start_of_local_day(held_back), anchor_kwh=80000.0
    )
    stored["last_cumulative_kwh"] = round(80000.0 + seen, 2)
    cost = stored["last_cumulative_cost"]

    portal.app["state"]["partial_day"] = False
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _sensor_value(hass, entry) == pytest.approx(
        80000.0 + _day_kwh(held_back) + _day_kwh(yesterday), abs=0.011
    )
    # The cost sensor never had such an anchor: it moves by what gets
    # written from here on.
    dollars = _held_back(held_back)[1]
    assert _sensor_value(hass, entry, "cost_usage") == pytest.approx(cost + dollars, abs=0.011)
    # What 0.13 stored is still there, untouched: going back carries on.
    now_stored = hass_storage[f"nbpower.{entry.entry_id}"]["data"]
    assert {key: now_stored[key] for key in left_by_0_13} == left_by_0_13


@pytest.mark.parametrize(
    ("stored", "external", "ledger"),
    [
        # Both stop at the same midnight: the difference is the offset.
        (
            {"sensor_anchor_at": "2026-11-09T00:00:00-04:00", "sensor_anchor_kwh": 70155.26},
            {"gen": EXTERNAL_GEN, "through": "2026-11-08", "energy_sum": 68021.5},
            {"chain": None, "offset": 2133.76},
        ),
        (
            {"sensor_anchor_at": "2026-11-09T00:00:00-04:00", "sensor_anchor_kwh": 70155.26},
            {"gen": EXTERNAL_GEN, "chain": "c1", "through": "2026-11-08", "energy_sum": 71000.0},
            {"chain": "c1", "offset": -844.74},
        ),
        # The statistics stop a day earlier than the old ledger did.
        (
            {"sensor_anchor_at": "2026-11-09T00:00:00-04:00", "sensor_anchor_kwh": 70155.26},
            {"gen": EXTERNAL_GEN, "through": "2026-11-07", "energy_sum": 68000.0},
            None,
        ),
        # Never settled a day: the anchor is the frontier, somewhere mid-day.
        (
            {"sensor_anchor_at": None, "sensor_anchor_kwh": None},
            {"gen": EXTERNAL_GEN, "through": "2026-11-08", "energy_sum": 68021.5},
            None,
        ),
        (
            {"sensor_anchor_at": "2026-11-09T12:40:00-04:00", "sensor_anchor_kwh": 70155.26},
            {"gen": EXTERNAL_GEN, "through": "2026-11-08", "energy_sum": 68021.5},
            None,
        ),
        # Statistics of another generation, about to be rebuilt; none at all.
        (
            {"sensor_anchor_at": "2026-11-09T00:00:00-04:00", "sensor_anchor_kwh": 70155.26},
            {"gen": EXTERNAL_GEN - 1, "through": "2026-11-08", "energy_sum": 68021.5},
            None,
        ),
        ({"sensor_anchor_at": "2026-11-09T00:00:00-04:00", "sensor_anchor_kwh": 70155.26}, {}, None),
    ],
)
def test_ledger_from_the_15_minute_feed(stored, external, ledger):
    """Where the energy sensor stands in the statistics, coming from 0.13;
    with nothing to go by, it follows no chain until a pass takes it up."""
    assert _ledger_from_interval_feed(stored, external) == (
        ledger or {"chain": "", "offset": 0.0}
    )


@pytest.mark.parametrize("anchor_days_off", [None, 1])
async def test_upgrade_without_a_usable_anchor_holds_the_energy_sensor(
    recorder_mock, hass, hass_storage, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations, anchor_days_off,
):
    """No settled anchor, or one that stops elsewhere than the statistics
    do: the sensor is not put back on the books, which it had left. It
    holds its value and moves with what is written from here on."""
    held_back = date.today() - timedelta(days=2)
    portal.app["state"]["partial_day"] = True
    entry = await _setup(hass, portal, nbpower_urls)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[f"nbpower.{entry.entry_id}"]["data"]
    if anchor_days_off is None:
        _as_stored_by_0_13(stored, anchor_at=None, anchor_kwh=None)
    else:
        _as_stored_by_0_13(
            stored,
            anchor_at=dt_util.start_of_local_day(held_back + timedelta(days=anchor_days_off)),
            anchor_kwh=39000.0,
        )
    # Well below the books' sum, as after a few dropped billing cycles.
    stored["last_cumulative_kwh"] = 40000.0

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _sensor_value(hass, entry) == 40000.0

    portal.app["state"]["partial_day"] = False
    await _refresh(hass, entry.runtime_data)
    assert _sensor_value(hass, entry) == pytest.approx(
        40000.0 + _held_back(held_back)[0], abs=0.011
    )


async def test_upgrade_takes_the_cost_sensor_up_where_it_stood(
    recorder_mock, hass, hass_storage, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    """Storage written by 0.12: no ledgers, session or sign-in history, a
    chain without a name, and the cost sensor held above what the books
    have come down to (live, since a cycle dropped out of them)."""
    held_back = date.today() - timedelta(days=2)
    portal.app["state"]["partial_day"] = True
    entry = await _setup(hass, portal, nbpower_urls)
    held_at = _sensor_value(hass, entry, "cost_usage") + 300
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[f"nbpower.{entry.entry_id}"]["data"]
    for key in ("energy_ledger", "cost_ledger", "session", "sign_in"):
        del stored[key]
    del stored["external"]["chain"]
    stored["last_cumulative_cost"] = held_at

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await nb.recorder_done(hass)
    # No step on the way in...
    assert _sensor_value(hass, entry, "cost_usage") == pytest.approx(held_at, abs=0.011)

    # ...and it moves again with the first hours that are published.
    portal.app["state"]["partial_day"] = False
    await _refresh(hass, entry.runtime_data)
    assert _sensor_value(hass, entry, "cost_usage") == pytest.approx(
        held_at + _held_back(held_back)[1], abs=0.011
    )


async def test_restart_resumes_without_rewriting_history(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations
):
    entry = await _setup(hass, portal, nbpower_urls)
    energy_id, _ = statistic_ids(entry, entry.runtime_data)
    before = await _rows(hass, energy_id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _refresh(hass, entry.runtime_data)
    after = await _rows(hass, energy_id)
    assert [(r["start"], r["sum"]) for r in after] == pytest.approx(
        [(r["start"], r["sum"]) for r in before]
    )


@pytest.mark.parametrize(
    ("day", "buckets", "doubled_hour"),
    [(date(2026, 3, 8), 23, 3), (date(2025, 11, 2), 24, None)],
)
def test_hour_buckets_keep_dst_day_totals(day, buckets, doubled_hour):
    """The portal labels DST days 00:00-23:00 like any other day."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("America/Halifax"))
    try:
        rows = [
            UsageRow(start=datetime(day.year, day.month, day.day, hour), kwh=1.0, amount=0.16)
            for hour in range(24)
        ]
        result = hour_buckets(rows)
    finally:
        dt_util.set_default_time_zone(previous)
    assert len(result) == buckets
    assert len({start.timestamp() for start, _, _ in result}) == buckets
    assert all(start.minute == 0 for start, _, _ in result)
    assert sum(kwh for _, kwh, _ in result) == pytest.approx(24.0)
    assert sum(cost for _, _, cost in result) == pytest.approx(24 * 0.16)
    if doubled_hour is not None:
        merged = [kwh for start, kwh, _ in result if start.hour == doubled_hour]
        assert merged == [pytest.approx(2.0)]


async def test_statistic_ids_keep_the_account_id_out_of_view(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations
):
    entry = await _setup(hass, portal, nbpower_urls)
    ids = statistic_ids(entry, entry.runtime_data)
    assert all(nb.ACCOUNT_NUMBER not in statistic_id for statistic_id in ids)
    meta = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids=set(ids))
    )
    assert sorted(m["name"] for _, m in meta.values()) == ["NB Power cost", "NB Power energy"]
    # The same account maps to the same ids, so a re-added entry continues them.
    again = MockConfigEntry(domain=DOMAIN, unique_id=f"account_{nb.ACCOUNT_NUMBER}")
    assert statistic_ids(again, entry.runtime_data) == ids
