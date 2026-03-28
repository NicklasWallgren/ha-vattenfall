"""Tests for Vattenfall backfill service handling."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import ModuleType
import importlib
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_dependency_stubs() -> None:
    """Install minimal stubs required to import integration __init__."""
    if "homeassistant" not in sys.modules:
        sys.modules["homeassistant"] = ModuleType("homeassistant")

    if "homeassistant.const" not in sys.modules:
        const_mod = ModuleType("homeassistant.const")
        const_mod.CONF_PASSWORD = "password"
        const_mod.Platform = type("Platform", (), {"SENSOR": "sensor"})
        sys.modules["homeassistant.const"] = const_mod

    if "homeassistant.config_entries" not in sys.modules:
        config_entries_mod = ModuleType("homeassistant.config_entries")

        class ConfigEntry:  # pragma: no cover - minimal stub
            pass

        config_entries_mod.ConfigEntry = ConfigEntry
        sys.modules["homeassistant.config_entries"] = config_entries_mod

    if "homeassistant.core" not in sys.modules:
        core_mod = ModuleType("homeassistant.core")

        class HomeAssistant:  # pragma: no cover - minimal stub
            pass

        class ServiceCall:
            def __init__(self, data=None):
                self.data = data or {}

        core_mod.HomeAssistant = HomeAssistant
        core_mod.ServiceCall = ServiceCall
        sys.modules["homeassistant.core"] = core_mod

    if "homeassistant.exceptions" not in sys.modules:
        exceptions_mod = ModuleType("homeassistant.exceptions")

        class HomeAssistantError(Exception):
            pass

        exceptions_mod.HomeAssistantError = HomeAssistantError
        sys.modules["homeassistant.exceptions"] = exceptions_mod

    if "homeassistant.helpers" not in sys.modules:
        sys.modules["homeassistant.helpers"] = ModuleType("homeassistant.helpers")

    if "homeassistant.helpers.config_validation" not in sys.modules:
        cv_mod = ModuleType("homeassistant.helpers.config_validation")
        cv_mod.date = lambda value: value
        cv_mod.string = lambda value: str(value)
        sys.modules["homeassistant.helpers.config_validation"] = cv_mod

    if "voluptuous" not in sys.modules:
        vol_mod = ModuleType("voluptuous")

        class _Schema:
            def __init__(self, schema):
                self.schema = schema

            def __call__(self, value):
                return value

        class _Marker:
            def __init__(self, key, default=None):
                self.key = key
                self.default = default

            def __hash__(self):
                return hash((self.key, self.default))

            def __eq__(self, other):  # noqa: ANN001
                return isinstance(other, _Marker) and (self.key, self.default) == (
                    other.key,
                    other.default,
                )

        vol_mod.Schema = _Schema
        vol_mod.Required = lambda key: _Marker(key)
        vol_mod.Optional = lambda key, default=None: _Marker(key, default=default)
        vol_mod.In = lambda values: (lambda value: value)
        sys.modules["voluptuous"] = vol_mod

    if "custom_components.vattenfall.api" not in sys.modules:
        api_mod = ModuleType("custom_components.vattenfall.api")

        class VattenfallApiClient:  # pragma: no cover - import-only stub
            pass

        api_mod.VattenfallApiClient = VattenfallApiClient
        sys.modules["custom_components.vattenfall.api"] = api_mod

    if "custom_components.vattenfall.coordinator" not in sys.modules:
        coordinator_mod = ModuleType("custom_components.vattenfall.coordinator")

        class VattenfallDataUpdateCoordinator:  # pragma: no cover - import-only stub
            pass

        coordinator_mod.VattenfallDataUpdateCoordinator = VattenfallDataUpdateCoordinator
        sys.modules["custom_components.vattenfall.coordinator"] = coordinator_mod


_install_dependency_stubs()
integration = importlib.import_module("custom_components.vattenfall")
const = importlib.import_module("custom_components.vattenfall.const")
HomeAssistantError = importlib.import_module("homeassistant.exceptions").HomeAssistantError


class _FakeCoordinator:
    def __init__(self, err: Exception | None = None) -> None:
        self.err = err
        self.calls: list[tuple[date, date, str]] = []

    async def async_backfill_range(self, start_date: date, end_date: date, mode: str) -> None:
        self.calls.append((start_date, end_date, mode))
        if self.err is not None:
            raise self.err


class _FakeCall:
    def __init__(self, data: dict) -> None:
        self.data = data


class _FakeHass:
    def __init__(self, domain_data: dict[str, dict]) -> None:
        self.data = {const.DOMAIN: domain_data}


class BackfillServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_backfill_rejects_invalid_date_range(self) -> None:
        hass = _FakeHass({})
        call = _FakeCall(
            {
                const.SERVICE_ATTR_START_DATE: date(2026, 3, 10),
                const.SERVICE_ATTR_END_DATE: date(2026, 3, 9),
                const.SERVICE_ATTR_MODE: "all",
            }
        )

        with self.assertRaises(HomeAssistantError) as ctx:
            await integration._async_handle_backfill_service(hass, call)

        self.assertIn("end_date must be on or after start_date", str(ctx.exception))

    async def test_backfill_rejects_future_end_date(self) -> None:
        hass = _FakeHass({"entry-1": {"coordinator": _FakeCoordinator()}})
        call = _FakeCall(
            {
                const.SERVICE_ATTR_START_DATE: date(2026, 3, 1),
                const.SERVICE_ATTR_END_DATE: date(2099, 1, 1),
                const.SERVICE_ATTR_MODE: "all",
            }
        )

        with self.assertRaises(HomeAssistantError) as ctx:
            await integration._async_handle_backfill_service(hass, call)

        self.assertIn("future", str(ctx.exception))

    async def test_backfill_targets_specific_entry_id(self) -> None:
        coordinator_1 = _FakeCoordinator()
        coordinator_2 = _FakeCoordinator()
        hass = _FakeHass(
            {
                "entry-1": {"coordinator": coordinator_1},
                "entry-2": {"coordinator": coordinator_2},
            }
        )
        call = _FakeCall(
            {
                const.SERVICE_ATTR_START_DATE: date(2026, 3, 1),
                const.SERVICE_ATTR_END_DATE: date(2026, 3, 7),
                const.SERVICE_ATTR_MODE: "hourly",
                const.SERVICE_ATTR_ENTRY_ID: "entry-2",
            }
        )

        await integration._async_handle_backfill_service(hass, call)

        self.assertEqual(coordinator_1.calls, [])
        self.assertEqual(coordinator_2.calls, [(date(2026, 3, 1), date(2026, 3, 7), "hourly")])

    async def test_backfill_temperature_mode_forwards_to_coordinator(self) -> None:
        coordinator = _FakeCoordinator()
        hass = _FakeHass({"entry-1": {"coordinator": coordinator}})
        call = _FakeCall(
            {
                const.SERVICE_ATTR_START_DATE: date(2026, 3, 1),
                const.SERVICE_ATTR_END_DATE: date(2026, 3, 7),
                const.SERVICE_ATTR_MODE: "temperature",
            }
        )

        await integration._async_handle_backfill_service(hass, call)

        self.assertEqual(
            coordinator.calls,
            [(date(2026, 3, 1), date(2026, 3, 7), "temperature")],
        )

    async def test_backfill_partial_failure_reports_failed_entry(self) -> None:
        coordinator_ok = _FakeCoordinator()
        coordinator_fail = _FakeCoordinator(RuntimeError("boom"))
        hass = _FakeHass(
            {
                "entry-ok": {"coordinator": coordinator_ok},
                "entry-fail": {"coordinator": coordinator_fail},
            }
        )
        call = _FakeCall(
            {
                const.SERVICE_ATTR_START_DATE: date(2026, 3, 1),
                const.SERVICE_ATTR_END_DATE: date(2026, 3, 7),
                const.SERVICE_ATTR_MODE: "all",
            }
        )

        with self.assertRaises(HomeAssistantError) as ctx:
            await integration._async_handle_backfill_service(hass, call)

        self.assertEqual(coordinator_ok.calls, [(date(2026, 3, 1), date(2026, 3, 7), "all")])
        self.assertEqual(coordinator_fail.calls, [(date(2026, 3, 1), date(2026, 3, 7), "all")])
        self.assertIn("entry-fail", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
