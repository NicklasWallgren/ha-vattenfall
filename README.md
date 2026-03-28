# Vattenfall Home Assistant integration

Custom integration for Home Assistant that retrieves electricity consumption from Vattenfall and exposes sensors for dashboards, Energy, and automations.

## Features

- Config flow setup from Home Assistant UI
- Hourly data updates via coordinator
- Consumption sensors:
  - Latest day consumption
  - Month-to-date consumption
  - Average daily consumption
  - Latest hour consumption
  - Today total consumption
  - Today peak hour consumption
- HACS compatible

## Installation

Add this repository to your HACS with the following button:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=nicklaswallgren&repository=ha-vattenfall&category=integration)

Install this integration with the following button:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=vattenfall)

### Requirements

- Home Assistant `2024.6.0` or later
- HACS (Home Assistant Community Store)

### Install with HACS (recommended)

1. Open HACS.
2. Go to `Integrations`.
3. Open the menu (`⋮`) and select `Custom repositories`.
4. Add this repository URL:
   - `https://github.com/nicklaswallgren/ha-vattenfall`
5. Select category `Integration`.
6. Search for `Vattenfall` in HACS and install it.
7. Restart Home Assistant.

### Manual install

1. Copy `custom_components/vattenfall` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.

## Configuration

1. In Home Assistant, go to `Settings` -> `Devices & Services`.
2. Click `Add Integration` and search for `Vattenfall`.
3. Enter:
   - `Customer ID`
   - `Password`
   - `Metering point ID`
   - `Subscription key`

## Notes

- The integration is configured to update consumption data every hour.
- Runtime dependency `httpx[http2]` is declared in `manifest.json` and installed by Home Assistant.
- Debug logs can include sensitive data (credentials/cookies/tokens). Do not share raw debug output.

## Backfill service

You can backfill historical consumption data from Home Assistant using the custom service:

- Service: `vattenfall.backfill`
- Fields:
  - `start_date` (required, `YYYY-MM-DD`)
  - `end_date` (required, `YYYY-MM-DD`)
  - `mode` (optional: `daily`, `hourly`, `both`, default `both`)
  - `entry_id` (optional: target one specific config entry)

Example call in Developer Tools -> Services:

```yaml
service: vattenfall.backfill
data:
  start_date: "2026-03-01"
  end_date: "2026-03-27"
  mode: both
```

## Development

Install test dependencies:

```bash
python3 -m pip install -r requirements-test.txt
```

Run unit tests:

```bash
python3 -m unittest discover -s tests -v
```

Run live API smoke tests:

```bash
bash tests/live_api_smoke.sh
python3 tests/live_api_smoke_api.py
```
