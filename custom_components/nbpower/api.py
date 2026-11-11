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
- ``Mode=H`` returns 24 hourly rows (``Hourly`` holds the time of day) for
  the day in ``strdate``/``date`` — about a year back — or the latest
  published day without one. The values are ``MI`` summed per hour. DST
  days keep the 00:00-23:00 labels: the spring-forward day carries a
  02:00 row that does not exist locally (2026-10).
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
- Token source (2026-10 probe): both portal pages that host the usage
  graph render a hidden ``accountSEWToken`` input — the account summary
  (where the graph is a conditional ``ucConsumptionGraph`` control that
  not every account gets) and ``/Customer/ViewConsumptionGraph.aspx``
  (the graph's own page, token at the top of the form). Each page mints
  its own token; both are accepted by VerifyToken, which is the widget's
  own bootstrap call per SEW's ``Usage_Widget.min.js`` — the hidden
  input is the browser's only token source (``/Token/GetToken`` answers
  404, GET and POST). A sibling ``accountSEWTokenError`` input carries
  the server's reason when minting failed for the account, and
  VerifyToken's data also carries ``AccountType`` — the ``UserType``
  usage requests are expected to send.
- Scheduled maintenance (2026-11-10/11, live): the portal switches its
  login form off. The page is served as usual, with the username,
  password and Login controls rendered ``disabled`` (class
  ``aspNetDisabled``) and a notice in its ``alertsContainer`` giving the
  window ("Scheduled Maintenance ... unavailable from Tuesday, November
  10 at 5:00pm until Wednesday, November 11 at 10:00pm"; such notices can
  also be up ahead of time, with the form still open). A sign-in posted
  anyway comes back as the login page again, exactly like a wrong
  password. That is what the six outages of October/November 2026 looked
  like from the inside (the earlier five were not captured), each ending
  only when the password was typed in again.
- The login page otherwise: the ``alertsContainer`` for messages, a
  Telerik RadWindowManager (``radalert``) for message boxes, an empty
  "Word Verification" fieldset (``fsCaptcha``) for a BotDetect CAPTCHA,
  and a hidden ``hdnCaptchaText`` preset to BotDetect's default text.
  What it answers to a wrong password or a required word verification
  has not been captured.
- The website sits behind Cloudflare and is not served to every country:
  from outside it answers HTTP 403 "Access Unavailable" (2026-11).

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
from bs4.element import CData, NavigableString

from .exceptions import (
    NBPowerApiError,
    NBPowerAuthError,
    NBPowerSignInClosed,
    NBPowerSignInRejected,
)

_LOGGER = logging.getLogger(__name__)

VALID_MODES = {"MI", "Mi", "H", "D", "M", "S"}  # 15-min (MI!), hourly, daily, monthly, seasonal
VALID_TYPES = {"K", "D"}  # K = kWh/kW, D = dollars

_ASPNET_JSON_DATE_RE = re.compile(r"^/Date\((\d+)(?:[+-]\d+)?\)/$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})")
_SEW_TOKEN_NAME_RE = re.compile(r"SEWToken$", re.IGNORECASE)
_SEW_TOKEN_ERROR_NAME_RE = re.compile(r"SEWTokenError$", re.IGNORECASE)

# Where login lands (the graph is a conditional control there, absent for
# some accounts), then the graph's own page — both render accountSEWToken.
_TOKEN_PAGES = (
    "/Customer/AccountSummaryView.aspx",
    "/Customer/ViewConsumptionGraph.aspx",
)

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

# How a page marks the places it reports problems in (the portal's own
# alerts sit in an ``alertsContainer``), and a word verification (the login
# form has a "Word Verification" fieldset that BotDetect fills in).
_MESSAGE_HINT_RE = re.compile(
    r"alert|error|invalid|fail|warn|validat|message|msg|notif|notice|summary"
    r"|status|feedback|danger",
    re.IGNORECASE,
)
_CAPTCHA_HINT_RE = re.compile(r"captcha|botdetect|BDC_|turnstile", re.IGNORECASE)
_HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE
)
# alert('...') and Telerik's radalert('...', width, ...): the portal's pages
# carry a RadWindowManager for message boxes.
_JS_ALERT_RE = re.compile(r"""\b(?:rad)?alert\(\s*(['"])(.+?)\1\s*[,)]""", re.DOTALL)
# Elements whose text is not something the page says to its reader, and
# ones too broad for their name to mark a message.
_NOT_SHOWN = ("script", "style", "title", "option", "textarea", "noscript")
_NOT_A_MESSAGE = ("html", "body", "form")
_REASON_LENGTH = 300


def _shape(body: Any) -> str:
    """Describe a payload without quoting it (it can hold account details)."""
    if isinstance(body, dict):
        return f"an object with keys {sorted(map(str, body))[:8]}"
    return f"a {type(body).__name__}"


def _describe(soup: BeautifulSoup, status: int) -> str:
    """A page in a few words, for the error about what it lacks."""
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return f"HTTP {status}, title {title!r}"


def _marks(element: Any, *attributes: str) -> str:
    """The element's values for the attributes, joined for a pattern search."""
    values: list[str] = []
    for attribute in attributes:
        value = element.get(attribute) or []
        values.extend([value] if isinstance(value, str) else map(str, value))
    return " ".join(values)


def _hidden(element: Any) -> bool:
    """True when the markup itself hides the element."""
    return element.get("hidden") is not None or bool(
        _HIDDEN_STYLE_RE.search(element.get("style") or "")
    )


def _page_text(soup: BeautifulSoup, *, messages_only: bool) -> list[str]:
    """The pieces of text a page shows, in order, each once.

    ``messages_only`` keeps the text of elements whose id, class or role
    marks them as alerts or validation messages, plus what scripts put in
    an ``alert()``.
    """
    pieces: list[str] = []
    for node in soup.find_all(string=True):
        # Exactly these: comments, scripts and styles are subclasses.
        if type(node) not in (NavigableString, CData):
            continue
        text = " ".join(node.split())
        if not text or text in pieces:
            continue
        shown = True
        marked = not messages_only
        for parent in node.parents:
            if parent.name in _NOT_SHOWN or _hidden(parent):
                shown = False
                break
            if (
                not marked
                and parent.name not in _NOT_A_MESSAGE
                and _MESSAGE_HINT_RE.search(_marks(parent, "id", "class", "role"))
            ):
                marked = True
        if shown and marked:
            pieces.append(text)
    for script in soup.find_all("script"):
        for match in _JS_ALERT_RE.finditer(script.string or ""):
            text = " ".join(match.group(2).split())
            if text and text not in pieces:
                pieces.append(text)
    return pieces


def _shows_word_verification(soup: BeautifulSoup) -> bool:
    """True when a page shows a CAPTCHA image or asks for its answer."""
    for element in soup.find_all(["img", "input", "iframe"]):
        if element.name == "input" and (element.get("type") or "text").lower() in (
            "hidden",
            "submit",
            "image",
            "button",
        ):
            continue
        if _CAPTCHA_HINT_RE.search(
            _marks(element, "id", "name", "class", "src")
        ) and not any(_hidden(node) for node in (element, *element.parents)):
            return True
    return False


def _quoted(pieces: list[str], secrets: tuple[str, ...] = ()) -> str | None:
    """Pieces of page text as one quotable string, without the secrets."""
    text = " ".join(pieces)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "…")
    if len(text) > _REASON_LENGTH:
        text = text[: _REASON_LENGTH - 1] + "…"
    return text or None


def _notice(page: BeautifulSoup) -> str | None:
    """What a page announces in its alert area (maintenance windows, say)."""
    return _quoted(_page_text(page, messages_only=True))


def _switched_off(page: BeautifulSoup, names: tuple[str, ...]) -> bool:
    """True when the page renders any of the named controls disabled."""
    return any(
        control.has_attr("disabled")
        for control in page.find_all(["input", "button"])
        if control.get("name") in names
    )


def _sign_in_rejected(
    login_page: BeautifulSoup, answer_html: str, secrets: tuple[str, ...]
) -> NBPowerSignInRejected:
    """Describe a sign-in the portal answered with its login page.

    The portal's reason is what the answer shows that the login page did
    not show before the sign-in (site-wide notices are on both): in its
    alert and validation elements if anything is new there, anywhere on
    the page otherwise. Failing that, the notices themselves are passed on
    as what the page had to say.
    """
    answer = BeautifulSoup(answer_html, "html.parser")
    new: list[str] = []
    for messages_only in (True, False):
        shown_before = set(_page_text(login_page, messages_only=messages_only))
        new = [
            text
            for text in _page_text(answer, messages_only=messages_only)
            if text not in shown_before
        ]
        if new:
            break
    reason = _quoted(new, secrets)
    if reason is None and "Cookies required" in answer_html:
        reason = "Cookies required"
    return NBPowerSignInRejected(
        reason,
        notice=_notice(login_page),
        challenged=_shows_word_verification(answer),
    )


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
        # The UserType usage requests carry; VerifyToken reports the real
        # AccountType per account (browsers send that too).
        self.user_type: str = "Residential"

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
        self.user_type = "Residential"

    def session_state(self) -> dict[str, Any] | None:
        """What it takes to go on using this sign-in later; None if signed out.

        The widget token works without the portal's cookies, so a client
        given this by :meth:`resume` needs no sign-in of its own until the
        token expires.
        """
        if not self.bootstrapped:
            return None
        return {
            "token": self.token,
            "account_number": self.account_number,
            "utility_account_number": self.utility_account_number,
            "user_type": self.user_type,
        }

    def resume(self, state: dict[str, Any]) -> None:
        """Take up a sign-in saved by :meth:`session_state`, unverified.

        The token may have expired since: the first request then raises
        NBPowerAuthError, as it does for any expired token.
        """
        self.invalidate()
        self.token = state.get("token")
        self.account_number = state.get("account_number")
        self.utility_account_number = state.get("utility_account_number")
        self.user_type = str(state.get("user_type") or "Residential")

    # ------------------------------------------------------------------
    # Public data access
    # ------------------------------------------------------------------

    async def bootstrap(
        self, username: str, password: str, *, validate: bool = True
    ) -> None:
        """Log in with portal credentials and resolve identifiers.

        The MyAccount login is the most fragile part of the flow (ASP.NET
        postback, a BotDetect word verification the portal can ask for).
        Only a sign-in the portal turns away raises NBPowerAuthError
        (NBPowerSignInRejected when it says so with its login page); any
        other failure is an NBPowerApiError — NBPowerSignInClosed when the
        login form is switched off for maintenance, in which case nothing
        is posted. ``validate=False`` skips the confirming usage request,
        for callers whose next request exercises the token anyway.
        """
        self.invalidate()
        await self._prime_session()
        await self._login(username, password)
        token = await self._account_token()
        try:
            # The page-scraped token type is accepted by VerifyToken, which
            # is what resolves the account identifiers in the login flow.
            data = await self._verify_token(token)
            await self.bootstrap_with_token(
                token,
                account_number=data.get("AccountNumber"),
                utility_account_number=data.get("UtilityAccountNumber"),
                user_type=str(data.get("AccountType") or "") or None,
                validate=validate,
            )
        except NBPowerAuthError as err:
            # The portal signed in and issued this token itself: refusing
            # it is the WidgetAPI's doing, no verdict on the credentials.
            self.invalidate()
            raise NBPowerApiError(
                f"The WidgetAPI refused the token the portal just issued: {err}"
            ) from err

    async def bootstrap_with_token(
        self,
        token: str,
        *,
        account_number: str | int | None = None,
        utility_account_number: str | int | None = None,
        user_type: str | None = None,
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
        NBPowerAuthError and a fresh token must be supplied. ``user_type``
        is the account's ``AccountType``, so the validating request already
        carries it rather than an assumed Residential.
        """
        self.invalidate()
        self.token = token
        self.account_number = account_number
        self.utility_account_number = utility_account_number
        if user_type:
            self.user_type = user_type
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
            "UserType": self.user_type or "Residential",
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
            raise NBPowerApiError(
                f"Unexpected usage payload: {_shape(result if result is not None else body)}"
            )
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

    async def get_hourly_usage(self, day: date) -> list[UsageRow]:
        """Fetch one day of hourly rows (24, with kWh and dollars).

        The same data as :meth:`get_interval_usage` summed per hour, and
        the same fallback: an unpublished day returns another day's rows,
        so rows are filtered to the requested day here.
        """
        payload = await self.get_usage(mode="H", rtype="K", day=day)
        return [
            row for row in parse_usage_rows(payload) if row.start.date() == day
        ]

    # ------------------------------------------------------------------
    # Portal session (ASP.NET WebForms)
    # ------------------------------------------------------------------

    async def _get_html(self, path: str) -> tuple[str, str]:
        """GET a portal page; return (html, final_url)."""
        html, final_url, _ = await self._get_page(path)
        return html, final_url

    async def _get_page(self, path: str) -> tuple[str, str, int]:
        """GET a portal page; return (html, final_url, status)."""
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
            return await resp.text(), str(resp.url), resp.status

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
        html, _, status = await self._get_page(path)
        soup = BeautifulSoup(html, "html.parser")
        fields, action = self._form_fields(soup)
        if action is None:
            # Not always a changed site: from outside the countries it
            # serves, the portal answers HTTP 403 "Access Unavailable".
            raise NBPowerApiError(
                f"Portal home page has no form ({_describe(soup, status)})"
            )
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
        html, _, page_status = await self._get_page(path)
        soup = BeautifulSoup(html, "html.parser")
        fields, action = self._form_fields(soup)
        if action is None:
            raise NBPowerApiError(
                f"Login page has no form ({_describe(soup, page_status)})"
            )

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

        # For scheduled maintenance the portal renders these controls
        # disabled and announces the window on the page. Posted anyway, the
        # form simply comes back — which is how a wrong password looks, and
        # what stopped the integration until 0.13. Post nothing.
        if _switched_off(soup, (user_field, pass_field, submit_field)):
            raise NBPowerSignInClosed(_notice(soup))

        data = dict(fields)
        data[user_field] = username
        data[pass_field] = password
        data[submit_field] = fields.get(submit_field, "Login")
        # The login form carries a BotDetect hidden field normally populated
        # client-side; keeping the scraped value (or the known default when
        # empty) has been observed to pass. When the portal does ask for
        # the word verification, the sign-in comes back rejected below.
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
            # Back on the login page. That is not proof of a wrong password
            # (see NBPowerSignInRejected), so pass on what the page said.
            raise _sign_in_rejected(soup, html2, (username, password))

    async def _account_token(self) -> str:
        """Scrape the SEW widget token from the first portal page with one.

        Both pages in ``_TOKEN_PAGES`` render the graph's hidden
        ``accountSEWToken`` field (each page mints its own token; SEW's
        Usage_Widget.min.js treats the hidden input as the token's only
        source — there is no mint endpoint, /Token/GetToken 404s). The
        account summary embeds the graph as a ``ucConsumptionGraph``
        control that not every account gets; the dedicated consumption
        page carries the token at the top of its form, so it is the second
        chance for accounts whose summary shows no graph. Each page also
        renders an ``accountSEWTokenError`` sibling the server fills in
        when it could not mint a token — the closest thing to an official
        reason, so it is surfaced in the error.
        """
        attempted: list[str] = []
        for path in _TOKEN_PAGES:
            html, final_url = await self._get_html(path)
            if "weblogin.aspx" in final_url.lower():
                raise NBPowerAuthError(
                    "NB Power accepted the sign-in, then asked to sign in again"
                )
            soup = BeautifulSoup(html, "html.parser")
            for el in soup.find_all("input", attrs={"name": _SEW_TOKEN_NAME_RE}):
                if token := el.get("value"):
                    return token
            detail = ""
            for el in soup.find_all("input", attrs={"name": _SEW_TOKEN_ERROR_NAME_RE}):
                if message := (el.get("value") or "").strip():
                    detail = f", server said {message!r}"
                    break
            if not detail:
                title = soup.title.get_text(" ", strip=True) if soup.title else ""
                detail = f", title {title!r}"
            attempted.append(f"{urlsplit(final_url).path!r}{detail}")
        raise NBPowerApiError(
            "No widget token on the account pages (" + "; ".join(attempted) + ")"
        )

    async def _verify_token(self, token: str) -> dict[str, Any]:
        body = await self._widget_post(
            "/Token/VerifyToken", {"Token": token}, authenticated=False
        )
        result = body.get("result") if isinstance(body, dict) else None
        data = result.get("Data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            message = result.get("Message") if isinstance(result, dict) else None
            raise NBPowerApiError(
                f"VerifyToken returned no account data, saying {message!r}"
                if message
                else f"VerifyToken returned {_shape(body)}"
            )
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
                message = str(result.get("Message") or "no message given")
                if "token" in message.lower() or "expired" in message.lower():
                    raise NBPowerAuthError(f"WidgetAPI token rejected: {message}")
                raise NBPowerApiError(f"WidgetAPI {path} error: {message}")
            return body
