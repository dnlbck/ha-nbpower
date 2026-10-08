"""End-to-end client tests against the mock portal server."""

from __future__ import annotations

from datetime import date, timedelta

import aiohttp
import pytest

from conftest import ACCOUNT_NUMBER, UTILITY_ACCOUNT_NUMBER, WIDGET_TOKEN, server_base
from custom_components.nbpower.api import (
    NBPowerClient,
    compute_cumulative,
    parse_tentative,
    parse_usage_rows,
)
from custom_components.nbpower.exceptions import NBPowerAuthError, NBPowerError


def _client(portal, session) -> NBPowerClient:
    return NBPowerClient(session, base_url=server_base(portal), widget_api_url=server_base(portal))


async def _bootstrap(portal, session, token=WIDGET_TOKEN) -> NBPowerClient:
    client = _client(portal, session)
    await client.bootstrap_with_token(
        token,
        account_number=ACCOUNT_NUMBER,
        utility_account_number=UTILITY_ACCOUNT_NUMBER,
    )
    return client


async def test_bootstrap_with_token(portal):
    """Bearer mode: validated via a usage request, identifiers stored."""
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        client = await _bootstrap(portal, session)
        assert client.bootstrapped
        assert str(client.account_number) == "1234567"
        assert client.utility_account_number == "9876543"


async def test_bootstrap_with_expired_token(portal):
    """Expired tokens arrive as HTTP 200 + result.Status=0 and raise AuthError."""
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        client = _client(portal, session)
        with pytest.raises(NBPowerAuthError, match="expired"):
            await client.bootstrap_with_token(
                "dead-token==",
                account_number=ACCOUNT_NUMBER,
                utility_account_number=UTILITY_ACCOUNT_NUMBER,
            )


async def test_bootstrap_rejects_wrong_account_numbers(portal):
    """The API 400s when the payload's account numbers don't match."""
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        client = _client(portal, session)
        with pytest.raises(NBPowerError):
            await client.bootstrap_with_token(
                WIDGET_TOKEN, account_number="999", utility_account_number="999"
            )


async def test_credential_login_still_works(portal):
    """The full login flow remains available for future use."""
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        client = _client(portal, session)
        await client.bootstrap("user@example.com", "s3cret")
        assert client.bootstrapped


async def test_bootstrap_rejects_bad_credentials(portal):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        client = _client(portal, session)
        with pytest.raises(NBPowerAuthError):
            await client.bootstrap("user@example.com", "wrong")


async def test_bootstrap_adopts_the_portals_account_type(portal):
    """VerifyToken reports AccountType; usage requests carry it as
    UserType instead of an assumed Residential."""
    portal.app["state"]["account_type"] = "General Service"
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        client = _client(portal, session)
        await client.bootstrap("user@example.com", "s3cret")
        assert client.user_type == "General Service"
        await client.get_daily_usage()
        assert portal.app["usage_calls"][-1]["UserType"] == "General Service"


async def test_interval_usage_returns_96_rows(client):
    """Mode=MI (uppercase) serves 15-minute intervals one day at a time."""
    from datetime import date, timedelta

    yesterday = date.today() - timedelta(days=1)
    rows = await client.get_interval_usage(yesterday)
    assert len(rows) == 96
    assert rows[0].start.hour == 0 and rows[0].start.minute == 0
    assert rows[-1].start.hour == 23 and rows[-1].start.minute == 45
    assert rows[0].start.date() == yesterday
    # Intervals sum to a plausible day total.
    assert 20 < sum(r.kwh for r in rows) < 100


async def test_interval_usage_out_of_range(client):
    from datetime import date, timedelta

    old = date.today() - timedelta(days=400)
    assert await client.get_interval_usage(old) == []


async def test_daily_usage_is_fixed_trailing_window(client):
    """Mode=D returns the trailing window (~1 cycle) with a ~4-day lag."""
    rows, tentative = await client.get_daily_usage()
    today = date.today()
    assert rows[0].start.date() == today - timedelta(days=30)
    assert rows[-1].start.date() == today - timedelta(days=4)
    for row in rows:
        assert row.status in ("Validated", "Estimated")
    # Non-monthly responses embed a zeroed placeholder month-to-date block;
    # the coordinator must use the monthly one instead.
    assert tentative["so_far_kwh"] == 0.0
    assert all(r.amount and r.amount > 0 for r in rows if r.kwh is not None)


async def test_monthly_usage_returns_cycles(client):
    """Mode=M returns billing-cycle rows dated via FromDate/ToDate."""
    rows, tentative = await client.get_monthly_usage()
    assert len(rows) == 36
    # Placeholder UsageDate must not be used; dates come from FromDate.
    assert all(row.start.year > 1000 for row in rows)
    assert all(row.end is not None and row.end > row.start for row in rows)
    # Sorted ascending by cycle start.
    starts = [r.start for r in rows]
    assert starts == sorted(starts)
    assert tentative["projected_kwh"] == pytest.approx(1440.0)


async def test_cumulative_derivation(client):
    """Cumulative = monthly cycles + daily rows after the last cycle end."""
    monthly, _ = await client.get_monthly_usage()
    daily, _ = await client.get_daily_usage()
    last_cycle_end = max(r.end.date() for r in monthly)
    post_cycle = [r for r in daily if r.start.date() > last_cycle_end]
    expected = sum(r.kwh for r in monthly) + sum(
        r.kwh for r in post_cycle if r.kwh is not None
    )
    cumulative, _ = compute_cumulative(
        [r for r in post_cycle if r.kwh is not None],
        base=round(sum(r.kwh for r in monthly), 3),
    )
    assert cumulative == pytest.approx(expected, abs=0.01)
    # The overlap day (== last cycle end) must be counted exactly once.
    assert daily[0].start.date() == last_cycle_end


async def test_hourly_usage_for_yesterday(client):
    payload = await client.get_usage(mode="H", rtype="K")
    rows = parse_usage_rows(payload)
    yesterday = date.today() - timedelta(days=1)
    assert len(rows) == 24
    assert all(r.start.date() == yesterday for r in rows)
    assert {r.start.hour for r in rows} == set(range(24))


async def test_monthly_tentative_real_semantics(client):
    _, tentative = await client.get_monthly_usage()
    assert tentative["so_far_kwh"] == pytest.approx(1388.0)
    assert tentative["so_far_cost"] == pytest.approx(219.89)
    # ProjectedBill carries the projected kWh; ExpectedUsage (0) is ignored.
    assert tentative["projected_kwh"] == pytest.approx(1440.0)
    assert tentative["projected_cost"] == pytest.approx(228.13)


async def test_widget_token_expiry_raises_auth(client, portal):
    portal.app["state"]["expire_widget_token"] = True
    with pytest.raises(NBPowerAuthError):
        await client.get_usage(mode="D", rtype="K", date_from="2026-09-01", date_to="2026-09-02")


async def test_rebootstrap_after_expiry(client, portal):
    portal.app["state"]["expire_widget_token"] = True
    with pytest.raises(NBPowerAuthError):
        await client.get_usage(mode="D", rtype="K", date_from="2026-09-01", date_to="2026-09-02")
    client.invalidate()
    assert not client.bootstrapped
    portal.app["state"]["expire_widget_token"] = False
    await client.bootstrap("user@example.com", "s3cret")
    rows = parse_usage_rows(
        await client.get_usage(mode="D", rtype="K", date_from="2026-09-01", date_to="2026-09-02")
    )
    assert rows  # the server ignores the dates and returns its window


async def test_usage_rejects_invalid_args(client):
    with pytest.raises(ValueError):
        await client.get_usage(mode="X")
    with pytest.raises(ValueError):
        await client.get_usage(rtype="Q")


async def test_usage_requires_bootstrap(portal):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        client = NBPowerClient(
            session, base_url=server_base(portal), widget_api_url=server_base(portal)
        )
        with pytest.raises(NBPowerAuthError):
            await client.get_usage(mode="D", rtype="K")


async def test_interval_usage_filters_fallback_rows(client):
    """The server returns the latest published day for unpublished days."""
    from datetime import date

    rows = await client.get_interval_usage(date.today())
    assert rows == []  # today's rows are actually yesterday's; filtered out
