"""Pure-function tests for payload parsing and date handling."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from custom_components.nbpower.api import (
    UsageRow,
    compute_cumulative,
    parse_portal_date,
    parse_tentative,
    parse_usage_rows,
)


def d(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-25", d(2026, 9, 25)),
        ("2026-09-25T13:45:00", d(2026, 9, 25).replace(hour=13, minute=45)),
        ("09/25/2026", d(2026, 9, 25)),
        ("25/09/2026", d(2026, 9, 25).replace(day=25)),  # DD/MM fallback
        ("Sep 25, 2026", d(2026, 9, 25)),
        ("/Date(1789272000000)/", datetime.fromtimestamp(1789272000000 / 1000)),
        (1789272000000, datetime.fromtimestamp(1789272000000 / 1000)),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_parse_portal_date(value, expected):
    assert parse_portal_date(value) == expected


def test_daily_rows_with_mixed_shapes():
    epoch_day = datetime.fromtimestamp(1789272000000 / 1000).date().isoformat()
    payload = {
        "result": {
            "Data": {
                "objUsageGenerationResultSetTwo": [
                    {"UsageDate": "2026-09-01", "Consumption": "12.3"},
                    {"UsageDate": "/Date(1789272000000)/", "UsageValue": "8,004.1"},
                    {"FromDate": "09/03/2026", "Consumption": 5},
                    {"UsageDate": "oops", "Consumption": 99},  # unparseable -> skipped
                    {"Consumption": 1},  # no date -> skipped
                ]
            }
        }
    }
    rows = parse_usage_rows(payload)
    assert [r.start.date().isoformat() for r in rows] == sorted(
        ["2026-09-01", epoch_day, "2026-09-03"]
    )
    by_date = {r.start.date().isoformat(): r.kwh for r in rows}
    assert by_date["2026-09-01"] == 12.3
    assert by_date[epoch_day] == 8004.1
    assert by_date["2026-09-03"] == 5.0


def test_data_may_be_a_json_string():
    data = {"objUsageGenerationResultSetTwo": [{"UsageDate": "2026-09-01", "Consumption": 4}]}
    payload = {"result": {"Data": json.dumps(data)}}
    rows = parse_usage_rows(payload)
    assert len(rows) == 1
    assert rows[0].kwh == 4.0


def test_mi_rows_merge_hourly_time():
    payload = {
        "result": {
            "Data": {
                "objUsageGenerationResultSetTwo": [
                    {"UsageDate": "2026-09-01", "Hourly": "13:15", "Consumption": 0.42},
                    {"UsageDate": "2026-09-01", "Hourly": "1:30", "Consumption": 0.18},
                ]
            }
        }
    }
    rows = parse_usage_rows(payload)
    assert rows[0].start.hour == 1 and rows[0].start.minute == 30
    assert rows[1].start.hour == 13 and rows[1].start.minute == 15


def test_monthly_cycle_rows_use_fromdate_not_placeholder():
    payload = {
        "result": {
            "Data": {
                "objUsageGenerationResultSetTwo": [
                    {
                        "UsageDate": "January 1, 0001",
                        "FromDate": "July 27, 2026",
                        "ToDate": "August 26, 2026",
                        "Consumption": 1774.0,
                        "Amount": 281.0,
                        "BillingDays": 31,
                    }
                ]
            }
        }
    }
    rows = parse_usage_rows(payload)
    assert len(rows) == 1
    assert rows[0].start == d(2026, 7, 27)
    assert rows[0].end == d(2026, 8, 26)
    assert rows[0].kwh == 1774.0
    assert rows[0].amount == 281.0


def test_tentative_projected_bill_is_kwh():
    """Live payloads: ExpectedUsage is 0; ProjectedBill holds projected kWh."""
    payload = {
        "result": {
            "Data": {
                "getTentativeData": [
                    {
                        "SoFar": 1388.0,
                        "ExpectedUsage": 0.0,
                        "ProjectedBill": 1440.0,
                        "ProjectedBillDollar": 228.13,
                        "SoFarDollar": 219.89,
                        "Average": 1874.0,
                        "Highest": 2640.0,
                    }
                ]
            }
        }
    }
    parsed = parse_tentative(payload)
    assert parsed["so_far_kwh"] == 1388.0
    assert parsed["projected_kwh"] == 1440.0
    assert parsed["projected_cost"] == 228.13
    assert parsed["typical_month_kwh"] == 1874.0
    assert parsed["highest_month_kwh"] == 2640.0


def test_tentative_falls_back_to_expected_usage():
    payload = {
        "result": {
            "Data": {
                "getTentativeData": [
                    {"SoFar": 100, "ExpectedUsage": 250, "ProjectedBillDollar": 40}
                ]
            }
        }
    }
    parsed = parse_tentative(payload)
    assert parsed["projected_kwh"] == 250.0


def test_tentative_empty_payload():
    assert parse_tentative({}) == {}
    assert parse_tentative({"result": {"Data": {}}}) == {}


def test_compute_cumulative_respects_anchor():
    rows = [
        UsageRow(start=d(2026, 9, 1), kwh=10),
        UsageRow(start=d(2026, 9, 2), kwh=20),
        UsageRow(start=d(2026, 9, 3), kwh=None),  # skipped
        UsageRow(start=d(2026, 9, 4), kwh=5),
    ]
    total, last = compute_cumulative(rows)
    assert total == 35
    assert last.isoformat() == "2026-09-04"

    # Anchored: rows on/before the anchor date are already counted in base.
    total, last = compute_cumulative(rows, base=100.5, through=rows[1].start.date())
    assert total == 105.5
    assert last.isoformat() == "2026-09-04"
