"""Async client for the NB Power customer portal and its WidgetAPI.

NB Power has no official public API. This client reproduces the requests the
MyAccount web portal makes — an ASP.NET form login on www.nbpower.com,
followed by calls to the underlying SEW WidgetAPI
(nbp-svc.smartcmobile.com) that the portal's usage graphs use. The wire
format was reverse-engineered by the nbpower-ha project
(https://github.com/noslent/nbpower-ha) and validated against live traffic
in September 2026 (see tests/fixtures/real/ for captured payloads).

Observed API behavior (2026-09):

- ``Mode=M`` returns ~36 monthly billing-cycle rows (deep history) plus the
  month-to-date ``getTentativeData`` block. Row dates come from
  ``FromDate``/``ToDate``; ``UsageDate`` is a placeholder in this mode.
- ``Mode=D`` returns a fixed trailing window (~one billing cycle) of daily
  rows regardless of the requested date range. ``UsageDate`` is real here.
  The last day or two carry ``ValidationStatus='Estimated'``.
- ``Mode=H`` returns hourly rows for yesterday only (``Hourly`` holds the
  time of day).
- ``Mode=Mi`` (15-minute) returns no rows for this account.
- Every response embeds ``getTentativeData``. In it, ``ProjectedBill`` is
  the projected kWh (``ExpectedUsage`` comes back 0) and
  ``Average``/``Highest`` are historical monthly kWh comparisons, not kW.
- The widget token the portal's graphs use is *rejected* by
  ``Token/VerifyToken`` ("Token has been expired.") even while it works
  fine on ``GetUsageGeneration`` — so token validity is checked with a
  lightweight usage request, not VerifyToken. Account numbers must be sent
  in the payload (HTTP 400 without them) and are not encoded in the token;
  the meter number may be empty.

This module must not import ``homeassistant`` so it can be unit tested and
driven standalone by ``scripts/test_client.py``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup

from .exceptions import NBPowerApiError, NBPowerAuthError

_LOGGER = logging.getLogger(__name__)

VALID_MODES = {"MI", "Mi", "H", "D", "M", "S"}  # 15-min (MI!), hourly, daily, monthly, seasonal
VALID_TYPES = {"K", "D"}  # K = kWh/kW, D = dollars

_ASPNET_JSON_DATE_RE = re.compile(r"^/Date\((\d+)(?:[+-]\d+)?\)/$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})")
_SEW_TOKEN_NAME_RE = re.compile(r"SEWToken$", re.IGNORECASE)

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%d-%b-%Y",
)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=45)


def _num(value: Any) -> float | None:
    """Coerce a portal value to float, tolerating None and thousand separators."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_portal_date(value: Any) -> datetime | None:
    """Parse a date/time value from the portal into a naive datetime.

    The API returns dates in several shapes depending on endpoint and mode:
    ISO strings, ``MM/DD/YYYY``, month names, and classic ASP.NET
    ``/Date(<epoch-ms>)/`` JSON dates.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Epoch milliseconds (bare number form).
        try:
            return datetime.fromtimestamp(float(value) / 1000)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if match := _ASPNET_JSON_DATE_RE.match(text):
        try:
            return datetime.fromtimestamp(int(match.group(1)) / 1000)
        except (OverflowError, OSError, ValueError):
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class UsageRow:
    """One normalized interval of usage data."""

    start: datetime
    kwh: float | None
    end: datetime | None = None  # billing-cycle rows span FromDate..ToDate
    kw: float | None = None
    amount: float | None = None
    status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


def parse_usage_rows(payload: dict[str, Any]) -> list[UsageRow]:
    """Normalize a GetUsageGeneration payload into UsageRows.

    Interval records live in ``objUsageGenerationResultSetTwo`` (sometimes
    ``...One``); ``Data`` may itself be a JSON-encoded string.
    """
    data = _extract_data(payload)
    if not isinstance(data, dict):
        return []
    series = (
        data.get("objUsageGenerationResultSetTwo")
        or data.get("objUsageGenerationResultSetOne")
        or []
    )
    if not isinstance(series, list):
        return []

    rows: list[UsageRow] = []
    for item in series:
        if not isinstance(item, dict):
            continue
        # Monthly rows carry the real span in FromDate/ToDate and a
        # placeholder ("January 1, 0001") in UsageDate; daily/hourly rows
        # have the real date in UsageDate.
        start = parse_portal_date(item.get("UsageDate"))
        if start is None or start.year <= 1000:
            start = parse_portal_date(
                item.get("FromDate") or item.get("WeatherUsageDate") or item.get("Date")
            )
        if start is None:
            _LOGGER.debug("Skipping usage row with unparseable date: %s", item)
            continue
        end = parse_portal_date(item.get("ToDate"))
        # 15-minute/hourly rows carry a time-of-day in a separate field.
        if (time_value := item.get("Hourly")) and (m := _TIME_RE.match(str(time_value))):
            start = start.replace(hour=int(m.group(1)) % 24, minute=int(m.group(2)))
        rows.append(
            UsageRow(
                start=start,
                kwh=_num(
                    item.get("Consumption")
                    if item.get("Consumption") is not None
                    else item.get("UsageValue")
                ),
                end=end,
                kw=_num(item.get("DemandValue") or item.get("MaxDemand")),
                amount=_num(item.get("Amount")),
                status=item.get("ValidationStatus"),
                raw=item,
            )
        )
    rows.sort(key=lambda r: r.start)
    return rows


def parse_tentative(payload: dict[str, Any]) -> dict[str, float | None]:
    """Extract the month-to-date/projection block (present in every response).

    Field semantics observed live: ``SoFar``/``SoFarDollar`` are month-to-date
    kWh and cost, ``ProjectedBill`` is the projected kWh (``ExpectedUsage``
    always returns 0), ``ProjectedBillDollar`` the projected bill, and
    ``Average``/``Highest`` compare against historical monthly kWh (not kW).
    """
    data = _extract_data(payload)
    entries = data.get("getTentativeData") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        return {}
    entry = entries[0]
    return {
        "so_far_kwh": _num(entry.get("SoFar")),
        "so_far_cost": _num(entry.get("SoFarDollar")),
        "projected_kwh": _num(
            entry.get("ProjectedBill")
            if entry.get("ProjectedBill") is not None
            else entry.get("ExpectedUsage")
        ),
        "projected_cost": _num(entry.get("ProjectedBillDollar")),
        "typical_month_kwh": _num(entry.get("Average")),
        "highest_month_kwh": _num(entry.get("Highest")),
    }


def _extract_data(payload: dict[str, Any]) -> Any:
    """Unwrap {result: {Data: ...}} where Data may be a JSON string."""
    result = (payload or {}).get("result")
    if not isinstance(result, dict):
        result = payload or {}
    data = result.get("Data", result)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    return data


def compute_cumulative(
    daily_rows: list[UsageRow],
    base: float = 0.0,
    through: date | None = None,
) -> tuple[float, date | None]:
    """Return (cumulative kWh, last covered date) over validated daily rows.

    ``base`` anchors the counter (sum of history fetched on first setup) and
    ``through`` excludes rows on/before it, which keeps the trailing-window
    re-fetch consistent as new days validate.
    """
    total = base
    last: date | None = None
    for row in daily_rows:
        if row.kwh is None:
            continue
        day = row.start.date()
        if through is not None and day <= through:
            continue
        total += row.kwh
        last = day
    return round(total, 3), last


class NBPowerClient:
    """Client for NB Power MyAccount and the underlying SEW WidgetAPI."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        widget_api_url: str,
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._widget_api = widget_api_url.rstrip("/")
        self.token: str | None = None
        self.account_number: str | None = None
        self.utility_account_number: str | None = None
        self.meter_number: str | None = None

    @property
    def bootstrapped(self) -> bool:
        """True when login completed and account identifiers are known."""
        return bool(self.token and self.account_number and self.utility_account_number)

    def invalidate(self) -> None:
        """Forget the session so the next call re-authenticates."""
        self.token = None
        self.account_number = None
        self.utility_account_number = None
        self.meter_number = None

    # ------------------------------------------------------------------
    # Public data access
    # ------------------------------------------------------------------

    async def bootstrap(
        self, username: str, password: str, *, validate: bool = True
    ) -> None:
        """Log in with portal credentials and resolve identifiers.

        The MyAccount login is the most fragile part of the flow (ASP.NET
        postback, a BotDetect field that would fail as an auth error if NB
        Power ever enforced the CAPTCHA). ``validate=False`` skips the
        confirming usage request, for callers whose next request exercises
        the token anyway.
        """
        self.invalidate()
        await self._prime_session()
        await self._login(username, password)
        token = await self._account_token()
        # The page-scraped token type is accepted by VerifyToken, which is
        # what resolves the account identifiers in the login flow.
        data = await self._verify_token(token)
        await self.bootstrap_with_token(
            token,
            account_number=data.get("AccountNumber"),
            utility_account_number=data.get("UtilityAccountNumber"),
            validate=validate,
        )

    async def bootstrap_with_token(
        self,
        token: str,
        *,
        account_number: str | int | None = None,
        utility_account_number: str | int | None = None,
        validate: bool = True,
    ) -> None:
        """Adopt a browser-captured widget token and validate it.

        Live testing (2026-09) showed VerifyToken *rejects* the token the
        portal's usage graphs use ("Token has been expired.", HTTP 200)
        even while that same token works fine on GetUsageGeneration — so
        validation goes through a lightweight usage request instead, and
        the account numbers must be supplied alongside the token (they are
        copied from the same browser request; the API rejects the payload
        with HTTP 400 without them, and the token does not encode them).

        Tokens expire after roughly a day; widget calls then raise
        NBPowerAuthError and a fresh token must be supplied.
        """
        self.invalidate()
        self.token = token
        self.account_number = account_number
        self.utility_account_number = utility_account_number
        if not (self.account_number and self.utility_account_number):
            raise NBPowerApiError(
                "Account number and utility account number are required "
                "alongside the token"
            )
        if validate:
            # A minimal daily-mode request must come back Status=1.
            await self.get_usage(mode="D", rtype="K")

    async def get_usage(
        self,
        *,
        mode: str = "D",
        rtype: str = "K",
        date_from: str | None = None,
        date_to: str | None = None,
        day: date | None = None,
    ) -> dict[str, Any]:
        """Fetch usage data and return the unwrapped payload.

        ``day`` selects a single day for interval modes (``MI``/``H``): it
        populates ``strdate`` as ``M/D/YYYY`` and ``date`` as the portal's
        long-form date, which is the shape the browser sends. Raises
        NBPowerAuthError when the widget token has expired.
        """
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        if rtype not in VALID_TYPES:
            raise ValueError(f"rtype must be one of {sorted(VALID_TYPES)}")
        if not self.bootstrapped:
            raise NBPowerAuthError("Client not bootstrapped")

        payload = {
            "Type": rtype,
            "Mode": mode,
            "strdate": f"{day.month}/{day.day}/{day.year}" if day else None,
            "enddate": None,
            "date": (
                f"{day.strftime('%B')} {day.day}, {day.year}" if day else None
            ),
            "hourlyType": "H",
            "seasonId": "",
            "weatherOverlay": 1,
            "usageyear": "",
            "MeterNumber": self.meter_number or "",
            "DateFromDaily": date_from or "",
            "DateToDaily": date_to or "",
            "IsNetUsage": "false",
            "IsNewUsageApi": "true",
            "usageType": "e",
            "LanguageCode": "EN",
            "Token": self.token,
            "AccountNumber": self.account_number,
            "UtilityAccountNumber": self.utility_account_number,
            "UserType": "Residential",
            "Uom": "kW",
            # Observed live as "RES_URBAN"; the reference project sent
            # "RES_RURAL" successfully, so the value appears not to be
            # validated server-side.
            "RatePlanCategoryName": "RES_URBAN",
        }
        body = await self._widget_post(
            "/Usage/GetUsageGeneration",
            payload,
            referer=f"{self._base}/",
        )
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict) or "Data" not in result:
            raise NBPowerApiError(f"Unexpected usage payload: {body}")
        return body

    async def get_monthly_usage(self) -> tuple[list[UsageRow], dict[str, float | None]]:
        """Fetch ~36 monthly billing-cycle rows plus the MTD summary."""
        payload = await self.get_usage(mode="M", rtype="K")
        return parse_usage_rows(payload), parse_tentative(payload)

    async def get_daily_usage(self) -> tuple[list[UsageRow], dict[str, float | None]]:
        """Fetch the trailing daily window (about one billing cycle).

        The server returns a fixed trailing window regardless of the
        requested dates, so no date filtering is attempted here.
        """
        payload = await self.get_usage(mode="D", rtype="K")
        return parse_usage_rows(payload), parse_tentative(payload)

    async def get_interval_usage(self, day: date) -> list[UsageRow]:
        """Fetch one day of 15-minute intervals (96 rows).

        Note the mode is uppercase ``MI``; ``Mi`` returns nothing. Available
        roughly one year back, one day per request; the server rejects
        multi-day ranges. Observed live: requesting a day that has not
        published yet returns the *latest published* day's rows instead of
        an empty set, so rows are filtered to the requested day here.
        """
        payload = await self.get_usage(mode="MI", rtype="K", day=day)
        return [
            row for row in parse_usage_rows(payload) if row.start.date() == day
        ]

    # ------------------------------------------------------------------
    # Portal session (ASP.NET WebForms)
    # ------------------------------------------------------------------

    async def _get_html(self, path: str) -> tuple[str, str]:
        """GET a portal page; return (html, final_url)."""
        url = f"{self._base}{path}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-CA,en;q=0.9",
            "Referer": f"{self._base}/",
        }
        async with self._session.get(
            url, headers=headers, timeout=_REQUEST_TIMEOUT, allow_redirects=True
        ) as resp:
            return await resp.text(), str(resp.url)

    async def _post_form(
        self, url: str, data: dict[str, str], referer: str
    ) -> tuple[str, str, int]:
        """POST an ASP.NET postback; return (html, final_url, status)."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self._base,
            "Referer": referer,
        }
        async with self._session.post(
            url,
            data=data,
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
            allow_redirects=True,
        ) as resp:
            return await resp.text(), str(resp.url), resp.status

    @staticmethod
    def _form_fields(soup: BeautifulSoup) -> tuple[dict[str, str], str | None]:
        """Collect named inputs from the first form plus its action URL."""
        form = soup.find("form")
        if form is None:
            return {}, None
        fields = {
            i.get("name"): i.get("value", "")
            for i in form.find_all("input")
            if i.get("name")
        }
        return fields, form.get("action")

    async def _prime_session(self) -> None:
        """Load the portal and select English to obtain a session cookie."""
        path = "/Default.aspx"
        html, _ = await self._get_html(path)
        soup = BeautifulSoup(html, "html.parser")
        fields, action = self._form_fields(soup)
        if action is None:
            raise NBPowerApiError("Portal home page has no form (site changed?)")
        data = {
            "__VIEWSTATE": fields.get("__VIEWSTATE", ""),
            "__VIEWSTATEGENERATOR": fields.get("__VIEWSTATEGENERATOR", ""),
            "__EVENTVALIDATION": fields.get("__EVENTVALIDATION", ""),
            "btnEnglish": "English",
        }
        post_url = urljoin(f"{self._base}{path}", action or path)
        _, _, status = await self._post_form(post_url, data, f"{self._base}{path}")
        if status not in (200, 302):
            raise NBPowerApiError(f"Session priming failed with HTTP {status}")

    async def _login(self, username: str, password: str) -> None:
        """Submit credentials to the WebForms login page."""
        path = "/auth/weblogin.aspx"
        login_url = f"{self._base}{path}"
        html, _ = await self._get_html(path)
        soup = BeautifulSoup(html, "html.parser")
        fields, action = self._form_fields(soup)
        if action is None:
            raise NBPowerApiError("Login page has no form (site changed?)")

        user_field = "ctl00$contentPlaceHolder$txtUsername"
        pass_field = "ctl00$contentPlaceHolder$txtPassword"
        submit_field = "ctl00$contentPlaceHolder$btnLogin"
        if user_field not in fields:
            if (el := soup.select_one('input[type="text"], input[type="email"]')) and (
                name := el.get("name")
            ):
                user_field = name
        if pass_field not in fields:
            if (el := soup.select_one('input[type="password"]')) and (
                name := el.get("name")
            ):
                pass_field = name
        if submit_field not in fields:
            if (
                el := soup.select_one('input[type="submit"], button[type="submit"]')
            ) and (name := el.get("name")):
                submit_field = name

        data = dict(fields)
        data[user_field] = username
        data[pass_field] = password
        data[submit_field] = fields.get(submit_field, "Login")
        # The login form carries a BotDetect hidden field normally populated
        # client-side; keeping the scraped value (or the known default when
        # empty) has been observed to pass. If NB Power starts enforcing the
        # CAPTCHA this will fail with an auth error.
        captcha_field = "ctl00$contentPlaceHolder$hdnCaptchaText"
        if captcha_field in data and not data[captcha_field]:
            data[captcha_field] = "BotDetect CAPTCHA ASP.NET Form Validation"
        for key in ("ctl00$txtSearchMobile", "ctl00$txtSearch"):
            if key in data:
                data[key] = ""

        post_url = urljoin(login_url, action or path)
        html2, final_url, status = await self._post_form(post_url, data, login_url)
        if status not in (200, 302):
            # A server error (e.g. 503 during portal maintenance) says
            # nothing about the credentials; an auth error here would stop
            # polling until the user re-entered their password.
            raise NBPowerApiError(f"Login POST returned HTTP {status}")
        if "weblogin.aspx" in final_url.lower() or "Cookies required" in html2:
            raise NBPowerAuthError("Invalid credentials or blocked login")

    async def _account_token(self) -> str:
        """Scrape the SEW widget token from the account summary page."""
        html, final_url = await self._get_html("/Customer/AccountSummaryView.aspx")
        if "weblogin.aspx" in final_url.lower():
            raise NBPowerAuthError("Session did not survive login")
        soup = BeautifulSoup(html, "html.parser")
        fields, _ = self._form_fields(soup)
        token = fields.get("ctl00$contentPlaceHolder$ucConsumptionGraph$accountSEWToken")
        if token:
            return token
        # The control path in the field name follows the page layout; the
        # name's tail is the stable part.
        for el in soup.find_all("input", attrs={"name": _SEW_TOKEN_NAME_RE}):
            if token := el.get("value"):
                return token
        # The WidgetAPI's /Token/GetToken (the reference project's fallback)
        # answers HTTP 404 since at least 2026-10, so there is nothing else
        # to try. Name the page the login landed on: an account picker or
        # an interstitial notice are the likely reasons for a missing graph.
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        raise NBPowerApiError(
            "No widget token on the account page (landed on "
            f"{urlsplit(final_url).path!r}, title {title!r})"
        )

    async def _verify_token(self, token: str) -> dict[str, Any]:
        body = await self._widget_post(
            "/Token/VerifyToken", {"Token": token}, authenticated=False
        )
        result = body.get("result") if isinstance(body, dict) else None
        data = result.get("Data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            raise NBPowerApiError(f"VerifyToken returned unexpected payload: {body}")
        return data

    # ------------------------------------------------------------------
    # WidgetAPI plumbing
    # ------------------------------------------------------------------

    async def _widget_post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        referer: str | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        """POST to the WidgetAPI; authenticated requests carry the token header."""
        return await self._widget_post_json(
            path, payload, referer=referer, authenticated=authenticated
        )

    async def _widget_post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        referer: str | None,
        authenticated: bool,
    ) -> dict[str, Any]:
        url = f"{self._widget_api}{path}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Content-Type": "application/json",
            "Origin": self._base,
            "Referer": referer or f"{self._base}/",
            # The portal's browser requests include these two headers empty;
            # sending them keeps our requests byte-identical to what the
            # WidgetAPI normally sees.
            "X-SEW": "",
            "Authorization": "",
        }
        if authenticated:
            if not self.token:
                raise NBPowerAuthError("No widget token")
            headers["Token"] = self.token

        async with self._session.post(
            url, json=payload, headers=headers, timeout=_REQUEST_TIMEOUT
        ) as resp:
            text = await resp.text()
            if resp.status in (401, 403):
                raise NBPowerAuthError(f"WidgetAPI rejected the token ({resp.status})")
            if resp.status != 200:
                raise NBPowerApiError(f"WidgetAPI {path} returned HTTP {resp.status}: {text[:200]}")
            try:
                body = json.loads(text)
            except json.JSONDecodeError as err:
                raise NBPowerApiError(
                    f"WidgetAPI {path} returned non-JSON response: {text[:200]}"
                ) from err
            # Errors arrive as HTTP 200 with result.Status=0 (e.g. an
            # expired token: "Token has been expired.").
            result = body.get("result") if isinstance(body, dict) else None
            if isinstance(result, dict) and result.get("Status") == 0:
                message = str(result.get("Message") or body)
                if "token" in message.lower() or "expired" in message.lower():
                    raise NBPowerAuthError(f"WidgetAPI token rejected: {message}")
                raise NBPowerApiError(f"WidgetAPI {path} error: {message}")
            return body
