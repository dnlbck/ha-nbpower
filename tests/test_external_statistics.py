"""The hourly external statistics the Energy dashboard reads."""

from __future__ import annotations

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
from custom_components.nbpower.api import UsageRow
from custom_components.nbpower.const import DOMAIN
from custom_components.nbpower.external_statistics import hour_buckets, statistic_ids

FLOW_INPUT = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "s3cret"}


async def _setup(hass, portal, nbpower_urls):
    nbpower_urls(nb.server_base(portal))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"NB Power {nb.ACCOUNT_NUMBER}",
        data=FLOW_INPUT,
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


async def test_upgraded_install_starts_the_statistics(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations
):
    """An entry whose sensor chain finished long ago still gets the books'
    history (and so the external statistics) on its next refresh."""
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    energy_id, cost_id = statistic_ids(entry, coord)
    assert coord.stored_stats_frontier() is not None
    first_chain = coord.external_state()["chain"]
    await coord.async_store_external_state({})
    coord.history_rows = []
    get_instance(hass).async_clear_statistics([energy_id, cost_id])
    await nb.recorder_done(hass)
    value = _sensor_value(hass, entry)
    cost = _sensor_value(hass, entry, "cost_usage")

    await _refresh(hass, coord)
    state = coord.external_state()
    assert state["through"] == (date.today() - timedelta(days=1)).isoformat()
    rows = await _rows(hass, energy_id)
    assert _local(rows[0]).date() < date.fromisoformat(state["window_start"])
    # The sensor is not part of this chain.
    assert _sensor_value(hass, entry) >= value
    # The cost sensor is, and takes a new chain up from where it stood.
    assert state["chain"] != first_chain
    assert coord.stored_state()["cost_ledger"]["chain"] == state["chain"]
    assert _sensor_value(hass, entry, "cost_usage") == pytest.approx(cost, abs=0.011)


async def test_cost_sensor_follows_the_statistics_not_the_books(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations,
    monkeypatch,
):
    """Live: the portal lists only the last 36 billing cycles. Each time a
    new one posts the oldest drops out and the books' sum falls by a whole
    cycle, so the cost sensor, held at its last value, stood still for
    weeks at a time."""
    held_back = date.today() - timedelta(days=2)
    portal.app["state"]["partial_day"] = True
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    before = _sensor_value(hass, entry, "cost_usage")

    # The oldest cycle drops out of the books (they total less than before)
    # while the portal publishes the hours it was holding back.
    cycles = nb._monthly_cycles
    monkeypatch.setattr(nb, "_monthly_cycles", lambda: cycles(35))
    portal.app["state"]["partial_day"] = False
    await _refresh(hass, coord)

    published = sum(
        cost for start, _, cost in _portal_hours(held_back) if 8 <= start.hour <= 15
    )
    assert cycles(36)[-1]["Amount"] > 100 > published > 0.5
    assert _sensor_value(hass, entry, "cost_usage") == pytest.approx(
        before + published, abs=0.011
    )


async def test_upgrade_takes_the_cost_sensor_up_where_it_stood(
    recorder_mock, hass, hass_storage, portal, nbpower_urls, patched_helper_session,
    enable_custom_integrations,
):
    """Storage written by 0.12: no cost ledger, session or sign-in history,
    a chain without a name, and the cost sensor held above what the books
    have come down to (live, since a cycle dropped out of them)."""
    held_back = date.today() - timedelta(days=2)
    portal.app["state"]["partial_day"] = True
    entry = await _setup(hass, portal, nbpower_urls)
    held_at = _sensor_value(hass, entry, "cost_usage") + 300
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[f"nbpower.{entry.entry_id}"]["data"]
    for key in ("cost_ledger", "session", "sign_in"):
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
    published = sum(
        cost for start, _, cost in _portal_hours(held_back) if 8 <= start.hour <= 15
    )
    assert _sensor_value(hass, entry, "cost_usage") == pytest.approx(
        held_at + published, abs=0.011
    )


async def test_cost_sensor_waits_for_a_pass_that_reaches_today(
    recorder_mock, hass, portal, nbpower_urls, patched_helper_session, enable_custom_integrations,
    monkeypatch,
):
    """A pass cut short has not counted every published hour; the total it
    got to says nothing about where the sensor should stand."""
    held_back = date.today() - timedelta(days=2)
    portal.app["state"]["partial_day"] = True
    entry = await _setup(hass, portal, nbpower_urls)
    coord = entry.runtime_data
    before = _sensor_value(hass, entry, "cost_usage")
    hourly = coord.client.get_hourly_usage

    async def _fails_on_yesterday(day):
        if day == date.today() - timedelta(days=1):
            raise aiohttp.ClientConnectionError("connection reset")
        return await hourly(day)

    monkeypatch.setattr(coord.client, "get_hourly_usage", _fails_on_yesterday)
    portal.app["state"]["partial_day"] = False
    await _refresh(hass, coord)
    assert _sensor_value(hass, entry, "cost_usage") == before

    monkeypatch.setattr(coord.client, "get_hourly_usage", hourly)
    await _refresh(hass, coord)
    published = sum(
        cost for start, _, cost in _portal_hours(held_back) if 8 <= start.hour <= 15
    )
    assert _sensor_value(hass, entry, "cost_usage") == pytest.approx(
        before + published, abs=0.011
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
