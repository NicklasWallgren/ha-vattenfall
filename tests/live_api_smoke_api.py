#!/usr/bin/env python3
"""Live smoke test that executes the integration API client directly."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
import getpass
import importlib
import logging
import os
from pathlib import Path
import sys
from types import ModuleType

# Ensure the repository root is importable so `custom_components` resolves.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _patch_dataclass_slots_for_py39() -> None:
    """Allow importing api.py on Python < 3.10 where dataclass(slots=...) is unsupported."""
    if sys.version_info >= (3, 10):
        return

    import dataclasses as dataclasses_mod

    original_dataclass = dataclasses_mod.dataclass

    def compatible_dataclass(*args, **kwargs):  # noqa: ANN002, ANN003
        kwargs.pop("slots", None)
        return original_dataclass(*args, **kwargs)

    dataclasses_mod.dataclass = compatible_dataclass


def _install_homeassistant_stubs() -> None:
    """Install minimal Home Assistant module stubs when HA is not installed."""
    if "homeassistant" not in sys.modules:
        sys.modules["homeassistant"] = ModuleType("homeassistant")

    if "homeassistant.const" not in sys.modules:
        const_mod = ModuleType("homeassistant.const")
        const_mod.CONF_PASSWORD = "password"
        const_mod.Platform = type("Platform", (), {"SENSOR": "sensor"})
        sys.modules["homeassistant.const"] = const_mod

    if "homeassistant.core" not in sys.modules:
        core_mod = ModuleType("homeassistant.core")

        class HomeAssistant:  # pragma: no cover - runtime compatibility only
            pass

        core_mod.HomeAssistant = HomeAssistant
        sys.modules["homeassistant.core"] = core_mod

    if "homeassistant.config_entries" not in sys.modules:
        config_entries_mod = ModuleType("homeassistant.config_entries")

        class ConfigEntry:  # pragma: no cover - runtime compatibility only
            pass

        config_entries_mod.ConfigEntry = ConfigEntry
        sys.modules["homeassistant.config_entries"] = config_entries_mod

    if "homeassistant.helpers" not in sys.modules:
        sys.modules["homeassistant.helpers"] = ModuleType("homeassistant.helpers")

    if "homeassistant.helpers.update_coordinator" not in sys.modules:
        update_mod = ModuleType("homeassistant.helpers.update_coordinator")

        class UpdateFailed(Exception):  # pragma: no cover - runtime compatibility only
            pass

        class DataUpdateCoordinator:  # pragma: no cover - runtime compatibility only
            def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
                pass

            def __class_getitem__(cls, _item):  # noqa: ANN001
                return cls

        update_mod.UpdateFailed = UpdateFailed
        update_mod.DataUpdateCoordinator = DataUpdateCoordinator
        sys.modules["homeassistant.helpers.update_coordinator"] = update_mod


async def _run() -> int:
    try:
        import httpx  # noqa: F401
    except Exception as err:  # noqa: BLE001
        print(f"ERROR: httpx is required to run this test: {err}", file=sys.stderr)
        return 2

    _patch_dataclass_slots_for_py39()
    _install_homeassistant_stubs()
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api = importlib.import_module("custom_components.vattenfall.api")

    customer_id = os.getenv("VATTENFALL_CUSTOMER_ID") or input("Customer ID: ").strip()
    password = os.getenv("VATTENFALL_PASSWORD") or getpass.getpass("Password: ").strip()

    metering_point_id = (
        os.getenv("VATTENFALL_METERING_POINT_ID")
        or input("Metering point ID: ").strip()
    )
    subscription_key = (
        os.getenv("VATTENFALL_SUBSCRIPTION_KEY")
        or input("Subscription key: ").strip()
    )

    if not customer_id or not password or not metering_point_id or not subscription_key:
        print(
            "ERROR: customerId, password, metering_point_id and subscription_key are required",
            file=sys.stderr,
        )
        return 2

    config = {
        "customer_id": customer_id,
        "password": password,
        "metering_point_id": metering_point_id,
        "subscription_key": subscription_key,
        "allow_stub_data": False,
    }

    client = None
    try:
        client = api.VattenfallApiClient(hass=object(), config=config)

        print("Running authentication flow via api.py ...")
        await client.async_authenticate(force=True)

        end_date = date.today()
        start_date = end_date - timedelta(days=7)

        print(
            f"Fetching consumption via api.py for {start_date.isoformat()} -> {end_date.isoformat()} ..."
        )
        daily_points = await client.async_get_daily_consumption(start_date, end_date)
        hourly_points = await client.async_get_hourly_consumption(
            start_date, end_date, include_load=True
        )

        if not daily_points:
            print("ERROR: API returned no points", file=sys.stderr)
            return 1
        if not hourly_points:
            print("ERROR: Hourly API returned no points", file=sys.stderr)
            return 1

        daily_points = sorted(daily_points, key=lambda p: p.date)
        daily_values = [p.value_kwh for p in daily_points]
        latest_daily = daily_points[-1]

        hourly_points = sorted(hourly_points, key=lambda p: p.date_time)
        hourly_values = [p.value_kwh for p in hourly_points]
        latest_hourly = hourly_points[-1]
        today_iso = date.today().isoformat()
        today_hourly = [p for p in hourly_points if p.date_time.startswith(today_iso)]
        today_total = sum(p.value_kwh for p in today_hourly)
        today_peak = max(today_hourly, key=lambda p: p.value_kwh) if today_hourly else None

        print("OK: live api.py smoke test passed")
        print(f"Metering point: {metering_point_id}")
        print(f"Daily range: {start_date.isoformat()} -> {end_date.isoformat()}")
        print(f"Daily points: {len(daily_points)}")
        print(f"Daily total kWh: {sum(daily_values):.3f}")
        print(f"Daily average kWh/day: {(sum(daily_values) / len(daily_values)):.3f}")
        print(f"Daily min kWh: {min(daily_values):.3f}")
        print(f"Daily max kWh: {max(daily_values):.3f}")
        print(f"Latest day: {latest_daily.date} = {latest_daily.value_kwh:.3f} kWh")

        print(f"Hourly points: {len(hourly_points)}")
        print(f"Latest hour: {latest_hourly.date_time} = {latest_hourly.value_kwh:.3f} kWh")
        print(f"Hourly min kWh: {min(hourly_values):.3f}")
        print(f"Hourly max kWh: {max(hourly_values):.3f}")
        print(f"Today total kWh: {today_total:.3f}")
        if today_peak is not None:
            print(f"Today peak hour: {today_peak.date_time} = {today_peak.value_kwh:.3f} kWh")
        return 0
    except Exception as err:  # noqa: BLE001
        print(f"ERROR: live api.py smoke test failed: {err}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            await client.async_close()


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
