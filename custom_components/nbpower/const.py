"""Constants for the NB Power integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "nbpower"

BASE_URL = "https://www.nbpower.com"
WIDGET_API_URL = "https://nbp-svc.smartcmobile.com/WidgetAPI"

# Currency reported by the portal (New Brunswick, Canada).
CURRENCY = "CAD"

CONF_SCAN_INTERVAL = "scan_interval"

# Portal data lags several days; polling more often than this is wasteful.
DEFAULT_SCAN_INTERVAL = timedelta(hours=4)
MIN_SCAN_INTERVAL = timedelta(minutes=30)

# Hourly-resolution backfill: 15-minute data is published ~1 year back,
# one day per API request. The pause between requests keeps the backfill
# polite; a full year takes a few minutes in the background.
INTERVAL_BACKFILL_DAYS = 365
INTERVAL_REQUEST_PAUSE = 0.25

# How many recent days to check for 15-minute data on every refresh. The
# daily books lag several days; the interval feed publishes intraday, and
# folding its newest days (strictly after the books' last day) into the
# cumulative sensor is what makes "today" advance between book updates.
MI_RECENT_DAYS = 7

CONF_BACKFILL_HOURLY = "backfill_hourly"

STORAGE_KEY = "nbpower"
STORAGE_VERSION = 1

# Bump to force one clean re-import of all statistics (day rows + hourly
# upgrade) after changes to the state-chaining convention.
REPAIR_GEN = 8
