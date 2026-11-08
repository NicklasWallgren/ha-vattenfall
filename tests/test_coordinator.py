"""Tests for Vattenfall coordinator data and statistics handling."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
import importlib
import sys
import unittest
from unittest.mock import AsyncMock, patch

import test_api_flow  # noqa: F401  # installs the Home Assistant stubs

coordinator_mod = importlib.import_module("custom_components.vattenfall.coordinator")
api = importlib.import_module("custom_components.vattenfall.api")

_START = datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()
_HOUR = 3600.0


class _FakeRecorder:
    """In-memory stand-in for the recorder statistics API."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def async_add_executor_job(self, func, *args):  # noqa: ANN001, ANN002
        return func(*args)

    def statistics_during_period(self, _hass, _start, _end, ids, *_rest):  # noqa: ANN001, ANN002
        return {statistic_id: self.rows for statistic_id in ids}


def _recorder_modules(recorder: _FakeRecorder) -> dict[str, ModuleType]:
    recorder_mod = ModuleType("homeassistant.components.recorder")
    recorder_mod.get_instance = lambda _hass: recorder
    models_mod = ModuleType("homeassistant.components.recorder.models")
    models_mod.StatisticData = dict
    statistics_mod = ModuleType("homeassistant.components.recorder.statistics")
    statistics_mod.statistics_during_period = recorder.statistics_during_period
    return {
        "homeassistant.components.recorder": recorder_mod,
        "homeassistant.components.recorder.models": models_mod,
        "homeassistant.components.recorder.statistics": statistics_mod,
    }


def _coordinator(client: object | None = None):
    entry = SimpleNamespace(entry_id="entry-1", data={"metering_point_id": "HDG1"})
    coordinator = coordinator_mod.VattenfallDataUpdateCoordinator(
        hass=SimpleNamespace(), client=client, entry=entry
    )
    coordinator.hass = SimpleNamespace()
    return coordinator


class HelperTests(unittest.TestCase):
    def test_hourly_timestamps_keep_repeated_dst_hour_apart(self) -> None:
        timestamps = coordinator_mod._hourly_timestamps(
            ["2026-10-25T01:00:00", "2026-10-25T02:00:00", "2026-10-25T02:00:00", "2026-10-25T03:00:00"]
        )

        self.assertEqual([b - a for a, b in zip(timestamps, timestamps[1:])], [_HOUR] * 3)

    def test_latest_day_ignores_today_and_prefers_full_days(self) -> None:
        full_day = [f"2026-03-01T{hour:02d}:00:00" for hour in range(24)]
        partial_day = ["2026-03-02T00:00:00"]
        today = ["2026-03-03T00:00:00"]

        self.assertEqual(
            coordinator_mod._latest_day(full_day + partial_day + today, before=date(2026, 3, 3)),
            "2026-03-01",
        )

    def test_latest_day_falls_back_to_partial_day_then_none(self) -> None:
        self.assertEqual(
            coordinator_mod._latest_day(["2026-03-02T00:00:00"], before=date(2026, 3, 3)),
            "2026-03-02",
        )
        self.assertIsNone(
            coordinator_mod._latest_day(["2026-03-03T00:00:00"], before=date(2026, 3, 3))
        )


class StatisticsMergeTests(unittest.IsolatedAsyncioTestCase):
    async def _merge(self, rows: list[dict], new_values: dict[float, float]) -> list[dict]:
        with patch.dict(sys.modules, _recorder_modules(_FakeRecorder(rows))):
            return await _coordinator()._async_merged_sum_statistics("vattenfall:x", new_values)

    async def test_backfilling_older_range_rebuilds_later_totals(self) -> None:
        later = _START + 100 * _HOUR
        rows = [{"start": later, "state": 2.0, "sum": 2.0}]

        stats = await self._merge(rows, {_START: 5.0})

        self.assertEqual([row["sum"] for row in stats], [5.0, 7.0])

    async def test_total_continues_across_a_gap(self) -> None:
        rows = [{"start": _START, "state": 4.0, "sum": 40.0}]

        stats = await self._merge(rows, {_START + 100 * _HOUR: 1.0})

        self.assertEqual([row["sum"] for row in stats], [41.0])

    async def test_new_values_replace_recorded_rows(self) -> None:
        rows = [
            {"start": _START, "state": 9.0, "sum": 9.0},
            {"start": _START + _HOUR, "state": 1.0, "sum": 10.0},
        ]

        stats = await self._merge(rows, {_START: 2.0})

        self.assertEqual([(row["state"], row["sum"]) for row in stats], [(2.0, 2.0), (1.0, 3.0)])


class CoordinatorDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_backfill_refuses_stub_data(self) -> None:
        client = SimpleNamespace(
            async_get_daily_consumption=AsyncMock(return_value=[]),
            async_get_hourly_consumption=AsyncMock(return_value=[]),
            async_get_hourly_temperature=AsyncMock(return_value=[]),
        )
        coordinator = _coordinator(client)
        coordinator.async_request_refresh = AsyncMock()

        await coordinator.async_backfill_range(date(2026, 3, 1), date(2026, 3, 2))

        for fetch in (
            client.async_get_daily_consumption,
            client.async_get_hourly_consumption,
            client.async_get_hourly_temperature,
        ):
            self.assertIs(fetch.await_args.kwargs["allow_stub"], False)

    async def test_latest_day_values_come_from_last_full_day(self) -> None:
        today = date(2026, 3, 3)
        full_day = today - timedelta(days=2)
        hourly = [
            api.HourlyConsumptionPoint(date_time=f"{full_day}T{hour:02d}:00:00", value_kwh=1.0)
            for hour in range(24)
        ]
        hourly.append(api.HourlyConsumptionPoint(date_time=f"{today}T00:00:00", value_kwh=9.0))
        client = SimpleNamespace(
            async_get_hourly_consumption=AsyncMock(return_value=hourly),
        )

        data = await _coordinator(client)._async_build_data(
            daily_start=None,
            daily_end=None,
            hourly_start=full_day,
            hourly_end=today,
            temperature_start=None,
            temperature_end=None,
        )

        self.assertEqual(data["today_total_kwh"], 24.0)
        self.assertEqual(data["today_peak_hour_kwh"], 1.0)
        self.assertEqual(data["latest_hour_kwh"], 9.0)
        self.assertEqual(data["hourly_latest_day"], full_day.isoformat())
        self.assertEqual(data["hourly_latest_day_reset"].date(), full_day)


if __name__ == "__main__":
    unittest.main()
