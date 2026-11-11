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

# The hourly part of the statistics: the portal serves hourly data about a
# year back, one day per request. The pause between requests keeps the
# first run polite; a full year takes a few minutes in the background.
HOURLY_DAYS = 365
REQUEST_PAUSE = 0.25

# The hourly part with the year-long backfill turned off. Wider than the
# portal's publication lag, so the days still being published are at hourly
# resolution either way; only older history stays at day resolution.
HOURLY_DAYS_WITHOUT_BACKFILL = 10

# Age (days) at which a day is taken as final: hours still missing by then
# are a permanent gap (meter outage), so the day stays as published instead
# of being fetched again on every refresh.
FINAL_AFTER_DAYS = 10

CONF_BACKFILL_HOURLY = "backfill_hourly"

# Waits after the portal turns a sign-in away (answers it with its login
# page again). That is not proof of a wrong password, so it is waited out
# and retried rather than reported as one at once. These four waits add up
# to the first day...
SIGN_IN_RETRY_DELAYS = (
    timedelta(hours=2),
    timedelta(hours=4),
    timedelta(hours=6),
    timedelta(hours=12),
)
# ...after which re-authentication is requested, while the sign-in is still
# tried once a day this many more times (a week in all) before giving up.
SIGN_IN_DAILY_RETRIES = 6

# Waits after a sign-in that could not be made or led nowhere: the portal
# has its login form switched off for maintenance (the usual case; a window
# has lasted 29 hours), answered with a 5xx or a page without the expected
# form, or the new session brought no data. No verdict on the credentials,
# so the last wait repeats for as long as it takes — but still not at the
# pace Home Assistant retries a setup.
SIGN_IN_ERROR_DELAYS = (
    timedelta(minutes=10),
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=2),
)

# Recent sign-ins kept for the diagnostics download.
SIGN_IN_EVENTS_KEPT = 20

STORAGE_KEY = "nbpower"
STORAGE_VERSION = 1

# Bump to clear and rebuild the statistics (the Energy dashboard's series)
# from scratch.
EXTERNAL_GEN = 1
