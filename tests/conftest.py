"""Test fixtures: a mock NB Power portal and WidgetAPI server."""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# The HA test plugin blocks sockets via pytest-socket, and Windows' default
# proactor event loop needs a socketpair just to initialize. The session
# event loop is created before any fixture can run, so the policy must be
# swapped at import time (this mirrors what HA core does on Windows).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # Windows event loops also construct a self-pipe socket on creation,
    # which pytest-socket blocks (Unix loops use os.pipe, so upstream HA
    # tests never hit this). Tests here talk to a localhost mock server on
    # purpose, so neuter the socket construction guard on Windows; the
    # plugin's DNS guard still applies and allows 127.0.0.1.
    import pytest_socket

    pytest_socket.disable_socket = lambda *a, **k: None  # noqa: ARG005

from custom_components.nbpower.api import NBPowerClient  # noqa: E402

VALID_CREDS = {"user@example.com": "s3cret"}
WIDGET_TOKEN = "token-abc123"
ACCOUNT_NUMBER = "1234567"
UTILITY_ACCOUNT_NUMBER = "9876543"
METER_NUMBER = "MTR001"

DEFAULT_FORM = """
<html><body><form action="{action}" method="post">
<input type="hidden" name="__VIEWSTATE" value="vs123"/>
<input type="hidden" name="__VIEWSTATEGENERATOR" value="gen456"/>
<input type="hidden" name="__EVENTVALIDATION" value="ev789"/>
{extra}
</form></body></html>
"""

LOGIN_FORM = DEFAULT_FORM.format(
    action="/auth/weblogin.aspx",
    extra="""
<input type="text" name="ctl00$contentPlaceHolder$txtUsername"/>
<input type="password" name="ctl00$contentPlaceHolder$txtPassword"/>
<input type="submit" name="ctl00$contentPlaceHolder$btnLogin" value="Login"/>
<input type="hidden" name="ctl00$contentPlaceHolder$hdnCaptchaText" value=""/>
<input type="text" name="ctl00$txtSearch" value=""/>
""",
)

ACCOUNT_FORM = DEFAULT_FORM.format(
    action="/Customer/AccountSummaryView.aspx",
    extra=(
        '<input type="hidden" '
        'name="ctl00$contentPlaceHolder$ucConsumptionGraph$accountSEWToken" '
        f'value="{WIDGET_TOKEN}"/>'
    ),
)


def _tentative_block() -> dict:
    """The real month-to-date block (only Mode=M carries real values)."""
    return {
        "Skey": 0,
        "UsageDate": None,
        "SoFar": 1388.0,
        "ExpectedUsage": 0.0,
        "PeakLoad": 0.0,
        "Average": 1874.0,
        "Highest": 2640.0,
        "ProjectedBill": 1440.0,
        "ProjectedBillDollar": 228.13,
        "SoFarDollar": 219.89,
    }


def _zeroed_tentative_block() -> dict:
    """The zeroed placeholder non-monthly responses embed."""
    return {
        "Skey": 0,
        "UsageDate": None,
        "SoFar": 0.0,
        "ExpectedUsage": 0.0,
        "PeakLoad": 0.0,
        "Average": 0.0,
        "Highest": 0.0,
        "ProjectedBill": 0.0,
        "ProjectedBillDollar": 0.0,
        "SoFarDollar": 0.0,
    }


def _monthly_cycles(count: int = 36) -> list[dict]:
    """Billing-cycle rows like Mode=M returns: dates in FromDate/ToDate.

    Cycles are contiguous and non-overlapping (e.g. Jun 26..Jul 26, then
    Jul 27..Aug 26), like the real portal's.
    """
    today = date.today()
    rows = []
    cycle_end = today - timedelta(days=30)
    for _ in range(count):
        cycle_start = cycle_end - timedelta(days=30)  # 31 days inclusive
        consumption = 1500 + (cycle_start.toordinal() % 500)
        rows.append(
            {
                "UsageDate": "January 1, 0001",  # placeholder in monthly mode
                "FromDate": cycle_start.strftime("%B %d, %Y"),
                "ToDate": cycle_end.strftime("%B %d, %Y"),
                "Consumption": float(consumption),
                "Amount": round(consumption * 0.158, 2),
                "BillingDays": 31,
                "Month": cycle_start.month,
                "Year": cycle_start.year,
                "ValidationStatus": None,
            }
        )
        cycle_end = cycle_start - timedelta(days=1)
    return rows


def _daily_window() -> list[dict]:
    """The fixed trailing daily window Mode=D returns, dates ignored."""
    today = date.today()
    window_start = today - timedelta(days=30)  # == last cycle's ToDate
    window_end = today - timedelta(days=4)  # ~4-day validation lag
    rows = []
    day = window_start
    while day <= window_end:
        kwh = None if day.day == 3 else 10.5 + (day.toordinal() % 30)
        rows.append(
            {
                "UsageDate": day.strftime("%B %d, %Y"),
                "Consumption": kwh if kwh is not None else "",
                "Amount": round((kwh or 0) * 0.158, 2),
                "ValidationStatus": (
                    "Estimated" if day >= window_end - timedelta(days=1) else "Validated"
                ),
                "BillingDays": 0,
            }
        )
        day += timedelta(days=1)
    return rows


def _interval_rows(day: date) -> list[dict]:
    """96 15-minute rows like Mode=MI returns for a published day."""
    rows = []
    for quarter in range(96):
        rows.append(
            {
                "UsageDate": day.strftime("%B %d, %Y"),
                "Hourly": f"{quarter // 4:02d}:{quarter % 4 * 15:02d}",
                "Consumption": round(0.3 + ((day.toordinal() * 96 + quarter) % 20) / 50, 4),
                "UsageValue": round(0.3 + ((day.toordinal() * 96 + quarter) % 20) / 50, 4),
                "ValidationStatus": "Validated",
            }
        )
    return rows


def _parse_strdate(value: str | None) -> date | None:
    if not value:
        return None
    try:
        month, day, year = (int(part) for part in value.split("/"))
        return date(year, month, day)
    except ValueError:
        return None


def _usage_payload(mode: str, rtype: str, strdate: str | None, enddate, date_from: str, date_to: str, state: dict | None = None) -> dict:
    """Build a GetUsageGeneration response the way the real API behaves.

    Daily consumption is derived from the date, so the same day always
    reports the same kWh regardless of how the client batches requests.
    """
    state = state or {}
    data: dict = {
        # Only monthly mode carries a real month-to-date block; other modes
        # embed a zeroed placeholder.
        "getTentativeData": [
            _tentative_block() if mode == "M" else _zeroed_tentative_block()
        ]
    }
    if mode == "M":
        data["objUsageGenerationResultSetTwo"] = _monthly_cycles()
    elif mode == "D":
        # DateFromDaily/DateToDaily are ignored server-side.
        rows = _daily_window()
        if state.get("revise_down"):
            # Simulate the portal revising estimated days downward.
            rows = [
                {**r, "Consumption": (r["Consumption"] * 0.5) if r["Consumption"] != "" else ""}
                for r in rows
            ]
        data["objUsageGenerationResultSetTwo"] = rows
    elif mode == "H":
        yesterday = date.today() - timedelta(days=1)
        data["objUsageGenerationResultSetTwo"] = [
            {
                "UsageDate": yesterday.strftime("%B %d, %Y"),
                "Hourly": f"{h:02d}:00",
                "Consumption": 0.9 + (h % 5) * 0.1,
                "UsageValue": 0.9 + (h % 5) * 0.1,
                "ValidationStatus": "Validated",
            }
            for h in range(24)
        ]
    elif mode.upper() == "MI":
        # Single day only; ranges (enddate) are rejected; 'Mi' (lowercase)
        # returns nothing like the real server; ~1 year of history. Today
        # publishes intraday — serve a partial day like the real feed.
        if mode != "MI" or enddate is not None:
            return {"result": {"Status": 0, "Message": "Invalid request", "Data": None}}
        day = _parse_strdate(strdate)
        today = date.today()
        if day is None or not (today - timedelta(days=365) <= day <= today):
            data["objUsageGenerationResultSetTwo"] = []
        else:
            rows = _interval_rows(day)
            data["objUsageGenerationResultSetTwo"] = (
                rows[:12] if day == today else rows
            )
    else:
        data["objUsageGenerationResultSetTwo"] = []  # S: not used
    return {"result": {"Data": data, "Status": 1}}


def build_app() -> web.Application:
    """Build the mock portal application with mutable test state."""
    app = web.Application()
    state: dict = {"expire_widget_token": False, "revise_down": False}

    # --- Portal pages -------------------------------------------------

    async def default_page(request: web.Request) -> web.Response:
        return web.Response(
            text=DEFAULT_FORM.format(action="/Default.aspx", extra=""),
            content_type="text/html",
        )

    async def default_post(request: web.Request) -> web.Response:
        form = await request.post()
        assert form.get("btnEnglish") == "English"
        resp = web.HTTPFound("/auth/weblogin.aspx")
        resp.set_cookie("sessionId", "primed")
        raise resp

    async def login_page(request: web.Request) -> web.Response:
        return web.Response(text=LOGIN_FORM, content_type="text/html")

    async def login_post(request: web.Request) -> web.Response:
        form = await request.post()
        username = form.get("ctl00$contentPlaceHolder$txtUsername", "")
        password = form.get("ctl00$contentPlaceHolder$txtPassword", "")
        if VALID_CREDS.get(username) == password:
            # A fresh login mints a fresh session token.
            state["expire_widget_token"] = False
            resp = web.HTTPFound("/Customer/AccountSummaryView.aspx")
            resp.set_cookie("auth", "1")
            raise resp
        # Failed logins re-render the login page (same URL) like the portal.
        return web.Response(text=LOGIN_FORM, content_type="text/html")

    async def account_page(request: web.Request) -> web.Response:
        if "auth" not in request.cookies:
            raise web.HTTPFound("/auth/weblogin.aspx")
        return web.Response(text=ACCOUNT_FORM, content_type="text/html")

    # --- WidgetAPI ----------------------------------------------------

    async def verify_token(request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("Token") != WIDGET_TOKEN:
            # Real API: HTTP 200 with result.Status=0 for a dead token.
            return web.json_response(
                {"result": {"Status": 0, "Message": "Token has been expired.", "Data": None}}
            )
        return web.json_response(
            {
                "version": "7.5.2",
                "responsestatus": 1,
                "result": {
                    "Status": 1,
                    "Message": "Record found",
                    "Data": {
                        "AccountNumber": int(ACCOUNT_NUMBER),
                        "UtilityAccountNumber": UTILITY_ACCOUNT_NUMBER,
                    },
                },
            }
        )

    async def multi_meter(request: web.Request) -> web.Response:
        if request.headers.get("Token") != WIDGET_TOKEN:
            return web.json_response(
                {"result": {"Status": 0, "Message": "Token has been expired.", "Data": None}}
            )
        return web.json_response({"result": {"MeterDetails": [{"MeterNumber": METER_NUMBER}]}})

    async def usage(request: web.Request) -> web.Response:
        if state["expire_widget_token"]:
            return web.json_response(
                {"result": {"Status": 0, "Message": "Token has been expired.", "Data": None}}
            )
        if request.headers.get("Token") != WIDGET_TOKEN:
            return web.json_response(
                {"result": {"Status": 0, "Message": "Token has been expired.", "Data": None}}
            )
        body = await request.json()
        # The real API rejects the payload with HTTP 400 when the account
        # identifiers are missing/wrong; they are not encoded in the token.
        if str(body.get("AccountNumber", "")) != ACCOUNT_NUMBER or str(
            body.get("UtilityAccountNumber", "")
        ) != UTILITY_ACCOUNT_NUMBER:
            return web.json_response(
                {"responsestatus": -1}, status=400
            )
        app["usage_calls"].append(
            {
                "Mode": body["Mode"],
                "Type": body["Type"],
                "strdate": body.get("strdate"),
                "DateFromDaily": body["DateFromDaily"],
                "DateToDaily": body["DateToDaily"],
            }
        )
        return web.json_response(
            _usage_payload(
                body["Mode"],
                body["Type"],
                body.get("strdate"),
                body.get("enddate"),
                body["DateFromDaily"],
                body["DateToDaily"],
                state,
            )
        )

    app["usage_calls"] = []
    app["state"] = state
    app.router.add_get("/Default.aspx", default_page)
    app.router.add_post("/Default.aspx", default_post)
    app.router.add_get("/auth/weblogin.aspx", login_page)
    app.router.add_post("/auth/weblogin.aspx", login_post)
    app.router.add_get("/Customer/AccountSummaryView.aspx", account_page)
    app.router.add_post("/Token/VerifyToken", verify_token)
    app.router.add_post("/Usage/GetMultiMeter", multi_meter)
    app.router.add_post("/Usage/GetUsageGeneration", usage)
    return app


@pytest_asyncio.fixture
async def portal(socket_enabled):
    """A running mock portal/WidgetAPI server."""
    server = TestServer(build_app())
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


def server_base(server: TestServer) -> str:
    return f"http://{server.host}:{server.port}"


@pytest.fixture
def nbpower_urls(monkeypatch):
    """Point every module that builds a client at a mock server base URL."""
    def _install(base: str) -> None:
        import custom_components.nbpower as nb_init
        from custom_components.nbpower import config_flow, const

        monkeypatch.setattr(const, "BASE_URL", base)
        monkeypatch.setattr(const, "WIDGET_API_URL", base)
        monkeypatch.setattr(nb_init, "BASE_URL", base)
        monkeypatch.setattr(nb_init, "WIDGET_API_URL", base)
        monkeypatch.setattr(config_flow, "BASE_URL", base)
        monkeypatch.setattr(config_flow, "WIDGET_API_URL", base)

    return _install


@pytest.fixture(autouse=True)
def fast_backfill(monkeypatch):
    """Keep the hourly backfill tiny and instant in all tests."""
    from custom_components.nbpower import statistics as nb_stats

    monkeypatch.setattr(nb_stats, "INTERVAL_BACKFILL_DAYS", 3)
    monkeypatch.setattr(nb_stats, "INTERVAL_REQUEST_PAUSE", 0)


@pytest.fixture
def hass_config_dir():
    """Point Home Assistant at the repo root so custom_components/ is found."""
    return str(REPO_ROOT)


@pytest_asyncio.fixture
async def patched_helper_session(monkeypatch):
    """Make HA's shared session tolerate the mock server's IP-literal host.

    The integration normally talks to nbpower.com (a real domain), where the
    default cookie jar works fine.
    """
    import custom_components.nbpower as nb_init
    from custom_components.nbpower import config_flow

    sessions: list[aiohttp.ClientSession] = []

    def _get(hass, *args, **kwargs):
        session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        sessions.append(session)
        return session

    monkeypatch.setattr(nb_init, "async_get_clientsession", _get)
    monkeypatch.setattr(config_flow, "async_get_clientsession", _get)
    yield _get
    for session in sessions:
        await session.close()


@pytest_asyncio.fixture
async def client(portal):
    """A client wired to the mock server, bootstrapped in bearer mode.

    The unsafe cookie jar is needed because the mock runs on an IP literal;
    aiohttp's default jar refuses cookies from hosts that aren't domains.
    """
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        c = NBPowerClient(session, base_url=server_base(portal), widget_api_url=server_base(portal))
        await c.bootstrap_with_token(
            WIDGET_TOKEN,
            account_number=ACCOUNT_NUMBER,
            utility_account_number=UTILITY_ACCOUNT_NUMBER,
        )
        yield c
