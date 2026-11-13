"""Tests for 15-minute consumption in the coordinator data payload."""

from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import unittest

from test_api_flow import api  # noqa: F401  # installs Home Assistant stubs

_COORDINATOR_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components" / "vattenfall" / "coordinator.py"
)


def _load_real_coordinator():
    """Load coordinator.py under its own name; other tests stub the regular module."""
    spec = importlib.util.spec_from_file_location(
        "custom_components.vattenfall._coordinator_under_test", _COORDINATOR_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


coordinator_mod = _load_real_coordinator()


class _FakeClient:
    def __init__(self, supports_quarter_hour: bool) -> None:
        self.supports_quarter_hour = supports_quarter_hour
        self.quarter_calls: list[tuple[date, date]] = []

    async def async_get_hourly_consumption(self, start, end, include_load=True):
        return []

    async def async_get_quarter_hour_consumption(self, start, end):
        self.quarter_calls.append((start, end))
        return [
            api.HourlyConsumptionPoint(date_time="2026-10-21T23:45:00", value_kwh=0.1),
            api.HourlyConsumptionPoint(date_time="2026-10-22T00:00:00", value_kwh=0.2),
            api.HourlyConsumptionPoint(date_time="2026-10-22T00:15:00", value_kwh=0.3),
        ]


def _coordinator(client: _FakeClient):
    coordinator = coordinator_mod.VattenfallDataUpdateCoordinator.__new__(
        coordinator_mod.VattenfallDataUpdateCoordinator
    )
    coordinator.client = client
    coordinator.data = None
    return coordinator


class TestCoordinatorQuarterHour(unittest.IsolatedAsyncioTestCase):
    """Validate the 15-minute part of the coordinator payload."""

    async def _build(self, client: _FakeClient) -> dict:
        return await _coordinator(client)._async_build_data(
            daily_start=None,
            daily_end=None,
            hourly_start=date(2026, 10, 21),
            hourly_end=date(2026, 10, 22),
            temperature_start=None,
            temperature_end=None,
        )

    async def test_latest_quarter_hour_and_latest_day_points(self) -> None:
        data = await self._build(_FakeClient(supports_quarter_hour=True))

        self.assertEqual(data["latest_quarter_hour_kwh"], 0.3)
        self.assertEqual(data["latest_quarter_hour_time"], "2026-10-22T00:15:00")
        self.assertEqual(
            [p["date_time"] for p in data["quarter_hour_points"]],
            ["2026-10-22T00:00:00", "2026-10-22T00:15:00"],
        )

    async def test_skipped_when_client_has_no_quarter_hour_data(self) -> None:
        client = _FakeClient(supports_quarter_hour=False)
        data = await self._build(client)

        self.assertNotIn("latest_quarter_hour_kwh", data)
        self.assertEqual(client.quarter_calls, [])


if __name__ == "__main__":
    unittest.main()
