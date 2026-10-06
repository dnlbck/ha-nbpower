# NB Power for Home Assistant

[![Validate with HACS Action](https://github.com/dnlbck/ha-nbpower/actions/workflows/hacs.yml/badge.svg)](https://github.com/dnlbck/ha-nbpower/actions/workflows/hacs.yml)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An unofficial Home Assistant custom integration that pulls electricity usage
from [NB Power](https://www.nbpower.com) (New Brunswick, Canada) and feeds it
into the **Energy dashboard**.

NB Power does not publish an official API. This integration signs in to
MyAccount (www.nbpower.com) with your username and password, exactly like a
browser does, and talks to the same SEW WidgetAPI
(`nbp-svc.smartcmobile.com`) the portal's usage graphs use. Portal sessions
are short-lived, so the integration logs in again automatically whenever
its session token expires — no manual token refreshing. The wire protocol
was reverse-engineered by the
[nbpower-ha](https://github.com/noslent/nbpower-ha) project and validated
against live traffic.

## Features

- Username/password sign-in with automatic session renewal (tokens expire
  within hours; the integration just logs in again)
- **Energy dashboard ready**: hourly energy and cost statistics
  (`NB Power <account> energy` / `cost`) that put every hour's usage in
  the hour it happened, as soon as the portal publishes it — even though
  the portal publishes a day or more late, and sometimes out of order
- **~3 years of history**: all published billing cycles (~36 months) at day
  resolution — each cycle's kWh spread evenly across its days — then the
  last ~12 months at hourly resolution, fetched by a resumable background
  task (one request per day, paced)
- A cumulative `total_increasing` kWh sensor advanced by the portal's
  interval feed, plus a cumulative CAD cost sensor
- Month-to-date usage and cost, projected usage and bill
- CAD monetary sensors, daily usage attributes

A refresh makes the monthly and daily requests plus one interval request
and one hourly request per day still being published (usually 1–3 each);
fully published days are settled and not fetched again. When the portal
session has expired, the sign-in adds a handful of requests.

## Entities

| Entity | Class | Description |
|---|---|---|
| `sensor.nb_power_energy_usage` | energy (kWh, total increasing) | Cumulative counter: the imported history, advanced by the 15-minute interval feed. Its usage lands at the poll that picks it up, not in the hour it was used — use the statistics below for the Energy dashboard. |
| `sensor.nb_power_cost_usage` | monetary (CAD, total) | Cumulative energy cost from the portal's books |
| `sensor.nb_power_last_daily_energy` | energy (kWh) | Most recent daily total in the portal's window |
| `sensor.nb_power_month_to_date_energy` | energy (kWh) | Month-to-date usage |
| `sensor.nb_power_month_to_date_cost` | monetary (CAD) | Month-to-date cost |
| `sensor.nb_power_projected_energy` | energy (kWh) | Projected monthly usage |
| `sensor.nb_power_projected_bill` | monetary (CAD) | Projected bill |

The energy usage sensor also exposes `account_number`, `meter_number`,
`last_daily_date`, and `daily_kwh` (the trailing window) as attributes.

### Statistics

| Statistic | Unit | Description |
|---|---|---|
| `nbpower:energy_<account>` (**NB Power \<account\> energy**) | kWh | **Use this for grid consumption.** Hourly usage for the last year, day resolution before that. |
| `nbpower:cost_<account>` (**NB Power \<account\> cost**) | CAD | **Use this as the consumption's cost.** The portal's per-hour dollars (energy charge only, rounded to the cent). |

These are written by the integration rather than recorded from a sensor:
the portal publishes usage a day or more late, so each refresh rewrites
the days still being published, and an hour that arrives late still lands
in its own hour.

## Installation

Requires Home Assistant 2025.11 or newer.

### HACS

1. HACS → ⋮ → **Custom repositories**
2. Add this repository, category **Integration**
3. Install **NB Power**, restart Home Assistant

### Manual

Copy `custom_components/nbpower/` into the `custom_components/` directory of
your Home Assistant configuration and restart.

## Configuration

1. **Settings → Devices & Services → Add Integration → NB Power**
2. Enter your NB Power MyAccount username and password — the account
   number, meter and widget token are resolved automatically at sign-in
3. First setup fetches the billing-cycle history and imports it as
   statistics; the hourly-resolution upgrade then runs in the background
   (a few minutes, one paced request per day, resumable)

If login fails with *invalid credentials*, sign in to www.nbpower.com once
in a normal browser and try again (NB Power occasionally challenges logins;
a recent browser session satisfies it). If it fails with *an unexpected
response*, the Home Assistant log names the sign-in step that failed and
the page the login landed on (search the log for `nbpower`) — please
include that line when opening an issue.

### Adding to the Energy dashboard

1. **Settings → Dashboards → Energy**
2. Under **Grid consumption**, click **Add consumption**
3. Select the statistic **NB Power \<account\> energy**
4. Under cost, choose **Use an entity tracking the total costs** and select
   **NB Power \<account\> cost**

Upgrading from 0.11 or earlier: edit the existing consumption and switch
it from *NB Power Energy usage* (the sensor) to the statistic above, and
the cost from *NB Power Cost usage* to *NB Power \<account\> cost*. The
statistics carry their own full history, so nothing is lost by switching.

Older periods (more than a year back) are each billing cycle's total
spread evenly across its days — daily views for those months are
estimates, while month and year views total exactly.

### Data resolution & freshness

- Hourly data (`Mode: "H"`) and 15-minute data (`Mode: "MI"` — uppercase!)
  are available for roughly the **last 12 months**, one day per request.
  The statistics use the hourly feed; the energy sensor the 15-minute one.
- The interval feeds publish a day's data a day or more after it ends,
  sometimes in blocks out of order (e.g. 00–07 and 16–23 before 08–15), so
  the Energy dashboard's "today" fills in the next day. Asking the portal
  for a day it has not published returns another day's data instead; the
  integration discards it.
- The daily books lag about **4 days** (the last days arrive marked
  *Estimated* and can be revised) and feed the month-to-date, projected
  and cost sensors.

### Options

The polling interval (1–24 hours, default 4) and the hourly-resolution
backfill (on by default) can be changed via **Configure** on the
integration entry. With the backfill off, only the last 10 days are
imported at hourly resolution (enough to bridge the portal's publication
lag); older history stays at daily resolution. The option applies when
the statistics are first built — history already imported stays as it is.

## Verifying the API before installing

To check that a token works before adding the integration (or to debug a
"rejected token" error), run the standalone harness (no Home Assistant
needed):

```bash
python -m venv .venv
.venv/Scripts/pip install aiohttp beautifulsoup4   # Windows
set NBPOWER_TOKEN=<paste your token>
.venv/Scripts/python scripts/test_client.py
```

It validates the token, pulls billing cycles, the daily window and hourly
data, derives the cumulative total, and writes raw JSON captures to
`scripts/out/` (gitignored — they contain your personal usage data).
`NBPOWER_USERNAME`/`NBPOWER_PASSWORD` also work if you want to exercise the
portal login flow.

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements_test.txt pytest-homeassistant-custom-component
# Windows: POSIX module shims + the repo root are needed for the HA test plugin
PYTHONPATH="$PWD/tests/windows_stubs" .venv/Scripts/python -m pytest
```

The test suite runs the client against a mock portal + WidgetAPI server and
full Home Assistant setup/config-flow/statistics tests
(`pytest-homeassistant-custom-component`). Live-captured payloads can be
dropped in `tests/fixtures/real/` to validate the parsers against real data
(`tests/test_real_payloads.py` skips when absent).

## Caveats

- **Unofficial.** NB Power does not publish this API and can change it at
  any time. Use a reasonable polling interval (the default 4 h is generous;
  the data updates about daily).
- Usage numbers are the portal's data and lag by a few days. For real-time
  monitoring you need local hardware (e.g. an Emporia Vue, Shelly EM, or an
  RTL-SDR listening to the meter's AMR transmissions).
- Multi-meter accounts: the first electricity meter is used.
- This project is not affiliated with NB Power.

## Credits

- [noslent/nbpower-ha](https://github.com/noslent/nbpower-ha) — the
  reverse-engineered login flow and WidgetAPI endpoints this project builds on.

## License

MIT — see [LICENSE](LICENSE).
