"""Data update coordinator for Vattenfall."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import VattenfallApiClient, VattenfallApiError
from .const import (
    ATTR_END_DATE,
    ATTR_HOURLY_END_DATE,
    ATTR_HOURLY_POINTS,
    ATTR_HOURLY_START_DATE,
    ATTR_POINTS,
    ATTR_START_DATE,
    ATTR_TEMPERATURE_END_DATE,
    ATTR_TEMPERATURE_POINTS,
    ATTR_TEMPERATURE_START_DATE,
    DEFAULT_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class VattenfallDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Handle fetching data from Vattenfall API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: VattenfallApiClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"vattenfall_{entry.entry_id}",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch latest data from API."""
        try:
            end_date = date.today() - timedelta(days=1)
            month_start = end_date.replace(day=1)
            hourly_end = date.today()
            hourly_start = hourly_end - timedelta(days=1)
            return await self._async_build_data(
                daily_start=month_start,
                daily_end=end_date,
                hourly_start=hourly_start,
                hourly_end=hourly_end,
                temperature_start=hourly_start,
                temperature_end=hourly_end,
            )
        except VattenfallApiError as err:
            raise UpdateFailed(f"Failed to fetch data from Vattenfall API: {err}") from err

    async def async_backfill_range(
        self,
        start_date: date,
        end_date: date,
        mode: str = "both",
    ) -> None:
        """Backfill integration data for a historical date range."""
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")

        data = dict(self.data)
        if mode in ("daily", "both"):
            partial = await self._async_build_data(
                daily_start=start_date,
                daily_end=end_date,
                hourly_start=None,
                hourly_end=None,
                temperature_start=None,
                temperature_end=None,
            )
            data.update(partial)

        if mode in ("hourly", "both"):
            partial = await self._async_build_data(
                daily_start=None,
                daily_end=None,
                hourly_start=start_date,
                hourly_end=end_date,
                temperature_start=None,
                temperature_end=None,
            )
            data.update(partial)

        if mode in ("temperature", "both"):
            partial = await self._async_build_data(
                daily_start=None,
                daily_end=None,
                hourly_start=None,
                hourly_end=None,
                temperature_start=start_date,
                temperature_end=end_date,
            )
            data.update(partial)

        data["backfill_mode"] = mode
        data["backfill_start_date"] = start_date.isoformat()
        data["backfill_end_date"] = end_date.isoformat()
        self.async_set_updated_data(data)

    async def _async_build_data(
        self,
        *,
        daily_start: date | None,
        daily_end: date | None,
        hourly_start: date | None,
        hourly_end: date | None,
        temperature_start: date | None,
        temperature_end: date | None,
    ) -> dict[str, Any]:
        """Build coordinator data payload for selected daily/hourly ranges."""
        data: dict[str, Any] = {}

        if daily_start is not None and daily_end is not None:
            daily_points = await self.client.async_get_daily_consumption(daily_start, daily_end)
            daily_values = [point.value_kwh for point in daily_points]

            latest_day_kwh = daily_values[-1] if daily_values else 0.0
            month_to_date_kwh = round(sum(daily_values), 3)
            avg_daily_kwh = (
                round(month_to_date_kwh / len(daily_values), 3) if daily_values else 0.0
            )

            data.update(
                {
                    "latest_day_kwh": latest_day_kwh,
                    "month_to_date_kwh": month_to_date_kwh,
                    "average_daily_kwh": avg_daily_kwh,
                    ATTR_START_DATE: daily_start.isoformat(),
                    ATTR_END_DATE: daily_end.isoformat(),
                    ATTR_POINTS: [asdict(point) for point in daily_points],
                }
            )

        if hourly_start is not None and hourly_end is not None:
            hourly_points = await self.client.async_get_hourly_consumption(
                hourly_start, hourly_end, include_load=True
            )
            hourly_values = [point.value_kwh for point in hourly_points]
            latest_hour_kwh = hourly_values[-1] if hourly_values else 0.0

            selected_day_iso = hourly_end.isoformat()
            selected_day_points = [
                p for p in hourly_points if p.date_time.startswith(selected_day_iso)
            ]
            selected_day_values = [p.value_kwh for p in selected_day_points]

            selected_day_total_kwh = round(sum(selected_day_values), 3)
            selected_day_peak_hour_kwh = max(selected_day_values) if selected_day_values else 0.0
            if selected_day_points:
                peak_point = max(selected_day_points, key=lambda p: p.value_kwh)
                selected_day_peak_hour_time = peak_point.date_time
            else:
                selected_day_peak_hour_time = None

            data.update(
                {
                    "latest_hour_kwh": latest_hour_kwh,
                    "today_total_kwh": selected_day_total_kwh,
                    "today_peak_hour_kwh": round(selected_day_peak_hour_kwh, 3),
                    "today_peak_hour_time": selected_day_peak_hour_time,
                    ATTR_HOURLY_START_DATE: hourly_start.isoformat(),
                    ATTR_HOURLY_END_DATE: hourly_end.isoformat(),
                    ATTR_HOURLY_POINTS: [asdict(point) for point in hourly_points],
                }
            )

        if temperature_start is not None and temperature_end is not None:
            temperature_points = await self.client.async_get_hourly_temperature(
                temperature_start, temperature_end, use_cet=True
            )
            selected_day_iso = temperature_end.isoformat()
            selected_day_temp_points = [
                p for p in temperature_points if p.date_time.startswith(selected_day_iso)
            ]
            selected_day_temp_values = [p.value_c for p in selected_day_temp_points]

            latest_temperature_c = (
                temperature_points[-1].value_c if temperature_points else None
            )
            today_avg_temperature_c = (
                round(sum(selected_day_temp_values) / len(selected_day_temp_values), 2)
                if selected_day_temp_values
                else None
            )
            today_min_temperature_c = (
                round(min(selected_day_temp_values), 2) if selected_day_temp_values else None
            )
            today_max_temperature_c = (
                round(max(selected_day_temp_values), 2) if selected_day_temp_values else None
            )

            data.update(
                {
                    "latest_temperature_c": latest_temperature_c,
                    "today_avg_temperature_c": today_avg_temperature_c,
                    "today_min_temperature_c": today_min_temperature_c,
                    "today_max_temperature_c": today_max_temperature_c,
                    ATTR_TEMPERATURE_START_DATE: temperature_start.isoformat(),
                    ATTR_TEMPERATURE_END_DATE: temperature_end.isoformat(),
                    ATTR_TEMPERATURE_POINTS: [asdict(point) for point in temperature_points],
                }
            )

        return data
