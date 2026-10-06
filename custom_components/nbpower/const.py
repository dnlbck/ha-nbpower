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

# Hourly window when the year-long backfill is turned off. The chain still
# has to reach the statistics frontier, so these recent days (wider than
# the daily books' publication lag) are always imported at hourly
# resolution; only older history stays at day resolution.
INTERVAL_RECENT_DAYS = 10

# Age (days) at which the 15-minute feed is treated as final: data still
# missing by then is a permanent gap (meter outage), so partial or empty
# days that old are settled or imported as published instead of awaited.
MI_SINCE_DAYS = 10

CONF_BACKFILL_HOURLY = "backfill_hourly"

STORAGE_KEY = "nbpower"
STORAGE_VERSION = 1

# Bump to force one clean re-import of all statistics (day rows + hourly
# upgrade) after changes to the state-chaining convention.
REPAIR_GEN = 9
