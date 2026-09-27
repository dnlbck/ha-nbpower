#!/usr/bin/env python3
"""Standalone harness to exercise the NB Power client with a live token.

Capture from your browser (sign in to www.nbpower.com, open the usage page,
F12 → Network, click a GetUsageGeneration request): the `Token` header and,
from the request payload, `AccountNumber` and `UtilityAccountNumber`.

    python -m venv .venv && .venv/Scripts/pip install aiohttp beautifulsoup4
    set NBPOWER_TOKEN=...
    set NBPOWER_ACCOUNT_NUMBER=123456
    set NBPOWER_UTILITY_ACCOUNT_NUMBER=00000987654321
    .venv/Scripts/python scripts/test_client.py

On success it writes JSON captures to scripts/out/ (gitignored — they
contain your personal usage data).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import aiohttp

# Load the client without executing nbpower/__init__.py, which imports
# homeassistant and would force a full HA install just to run this script.
_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "nbpower"
if "nbpower_standalone" not in sys.modules:
    _pkg = types.ModuleType("nbpower_standalone")
    _pkg.__path__ = [str(_COMPONENT)]
    sys.modules["nbpower_standalone"] = _pkg

from nbpower_standalone.api import NBPowerClient, parse_usage_rows  # noqa: E402
from nbpower_standalone.const import BASE_URL, WIDGET_API_URL  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "out"


def dump(name: str, obj: object) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    print(f"  wrote {path}")


async def main() -> int:
    token = os.environ.get("NBPOWER_TOKEN")
    account = os.environ.get("NBPOWER_ACCOUNT_NUMBER")
    utility = os.environ.get("NBPOWER_UTILITY_ACCOUNT_NUMBER")
    username = os.environ.get("NBPOWER_USERNAME")
    password = os.environ.get("NBPOWER_PASSWORD")
    if not (
        (token and account and utility) or (username and password)
    ):
        print(
            "Set NBPOWER_TOKEN + NBPOWER_ACCOUNT_NUMBER + "
            "NBPOWER_UTILITY_ACCOUNT_NUMBER (recommended), or "
            "NBPOWER_USERNAME and NBPOWER_PASSWORD."
        )
        return 2

    async with aiohttp.ClientSession() as session:
        client = NBPowerClient(session, base_url=BASE_URL, widget_api_url=WIDGET_API_URL)

        print("1. Bootstrapping...")
        if token:
            await client.bootstrap_with_token(
                token, account_number=account, utility_account_number=utility
            )
        else:
            await client.bootstrap(username, password)
        print(f"   account={client.account_number} "
              f"utility={client.utility_account_number}")

        print("2. Fetching monthly billing cycles + month-to-date...")
        monthly, tentative = await client.get_monthly_usage()
        dump("monthly_raw", {"rows": [r.raw for r in monthly]})
        print(f"   {len(monthly)} cycles, {monthly[0].start.date()}..{monthly[-1].end.date()}")
        print(f"   MTD: {tentative}")

        print("3. Fetching the trailing daily window...")
        daily, _ = await client.get_daily_usage()
        dump("daily_raw", {"rows": [r.raw for r in daily]})
        total = sum(r.kwh for r in daily if r.kwh is not None)
        print(f"   {len(daily)} days {daily[0].start.date()}..{daily[-1].start.date()}, "
              f"{total:.0f} kWh, last status={daily[-1].status!r}")

        print("4. Fetching 15-minute data for yesterday (Mode=MI)...")
        intervals = await client.get_interval_usage(date.today() - timedelta(days=1))
        dump("interval_raw", {"rows": [r.raw for r in intervals]})
        if intervals:
            day_total = sum(r.kwh for r in intervals)
            print(f"   {len(intervals)} intervals, {day_total:.2f} kWh, "
                  f"first at {intervals[0].start:%H:%M}")
        else:
            print("   no intervals published for yesterday (try an older day)")

        cumulative = sum(r.kwh for r in monthly) + sum(
            r.kwh
            for r in daily
            if r.kwh is not None and r.start.date() > monthly[-1].end.date()
        )
        print(f"5. Derived cumulative total: {cumulative:.0f} kWh")

    print("All steps succeeded — the integration's API flow is working.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
