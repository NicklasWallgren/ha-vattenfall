# Vattenfall Home Assistant integration

Custom integration for Home Assistant that retrieves electricity consumption from Vattenfall and exposes sensors for
dashboards, Energy, and automations.

> **Note:** This integration only works with Vattenfall Eldistribution in Sweden (`vattenfalleldistribution.se`). It is
> not compatible with Vattenfall in other countries.

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
  - Latest outdoor temperature
  - Today average temperature
  - Today minimum temperature
  - Today maximum temperature
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
   - `Temperature area code` (defaults to `14132`)

### Getting a subscription key

The integration authenticates against Vattenfall's API, which requires an Azure API Management
`Ocp-Apim-Subscription-Key` header. This key isn't issued anywhere in account self-service — the web portal's
frontend uses it internally. You can retrieve it yourself using your browser's developer tools (see
[issue #1](https://github.com/NicklasWallgren/ha-vattenfall/issues/1)):

1. Log in to [vattenfalleldistribution.se](https://vattenfalleldistribution.se) in your browser.
2. Open developer tools (`F12`) and switch to the `Network` tab.
3. Browse to your consumption data so the page makes API requests.
4. Find a request to the Vattenfall API and inspect its request headers.
5. Copy the value of the `Ocp-Apim-Subscription-Key` header — this is your `Subscription key`.

## Notes

- The integration is configured to update consumption data every hour.
- Runtime dependency `httpx[http2]` is declared in `manifest.json` and installed by Home Assistant.
- Debug logs can include sensitive data (credentials/cookies/tokens). Do not share raw debug output.

## Backfill action

You can backfill historical consumption data from Home Assistant using the custom action:

- Action: `vattenfall.backfill`
- Fields:
  - `start_date` (required, `YYYY-MM-DD`)
  - `end_date` (required, `YYYY-MM-DD`)
  - `mode` (optional: `daily`, `hourly`, `temperature`, `all`, default `all`)
  - `entry_id` (optional: target one specific config entry)

Example call in `Settings` -> `Tools` -> `Actions` (previously `Developer Tools` -> `Services`):

```yaml
action: vattenfall.backfill
data:
  start_date: "2025-04-11"
  end_date: "2026-08-18"
  mode: all
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
