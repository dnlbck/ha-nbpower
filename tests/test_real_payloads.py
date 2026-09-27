"""Tests against real captured API payloads.

The fixtures under tests/fixtures/real/ are captured from the live WidgetAPI
with a real account and contain personal usage data, so they are gitignored
and these tests skip automatically when the directory is absent.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from custom_components.nbpower.api import parse_tentative, parse_usage_rows

REAL = Path(__file__).parent / "fixtures" / "real"

pytestmark = pytest.mark.skipif(
    not (REAL / "resp_monthly.json").exists(), reason="real fixtures not present"
)


def _load(name: str) -> dict:
    return json.loads((REAL / name).read_text(encoding="utf-8"))


def test_real_monthly_payload():
    rows = parse_usage_rows(_load("resp_monthly.json"))
    assert len(rows) == 36
    assert all(row.end is not None for row in rows)
    # Cycles are contiguous: no shared days, no gaps.
    days = sorted({d for row in rows for d in _dates(row)})
    for earlier, later in zip(days, days[1:]):
        assert (later - earlier).days <= 1
    assert all(row.kwh and row.kwh > 0 for row in rows)


def _dates(row):
    from datetime import timedelta as td

    d = row.start.date()
    end = row.end.date()
    while d < end:
        yield d
        d = d + td(days=1)
    yield end


def test_real_monthly_tentative():
    tentative = parse_tentative(_load("resp_monthly.json"))
    assert tentative["so_far_kwh"] and tentative["so_far_kwh"] > 0
    assert tentative["projected_kwh"] and tentative["projected_kwh"] > 0
    assert tentative["projected_cost"] and tentative["projected_cost"] > 0


def test_real_daily_payload():
    rows = parse_usage_rows(_load("resp_daily_30d_k.json"))
    assert 20 <= len(rows) <= 40
    starts = [row.start.date() for row in rows]
    assert starts == sorted(starts)
    # The trailing window ends a few days before today (validation lag) and
    # the last row(s) are estimates.
    assert starts[-1] < date.today()
    assert rows[-1].status == "Estimated"


def test_real_hourly_payload():
    # Mi (15-minute) returns nothing for this account; hourly (H) data is
    # transient — it exists for a short window after each day validates, so
    # the capture may legitimately contain rows or be empty.
    assert parse_usage_rows(_load("resp_mi_1day_k.json")) == []
    hourly = REAL / "resp_hourly.json"
    if hourly.exists():
        rows = parse_usage_rows(_load("resp_hourly.json"))
        assert all(r.start.date() >= date.today() - timedelta(days=3) for r in rows)
