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

            daily_points = await self.client.async_get_daily_consumption(month_start, end_date)
            daily_values = [point.value_kwh for point in daily_points]

            latest_day_kwh = daily_values[-1] if daily_values else 0.0
            month_to_date_kwh = round(sum(daily_values), 3)
            avg_daily_kwh = (
                round(month_to_date_kwh / len(daily_values), 3) if daily_values else 0.0
            )

            hourly_end = date.today()
            hourly_start = hourly_end - timedelta(days=1)
            hourly_points = await self.client.async_get_hourly_consumption(
                hourly_start, hourly_end, include_load=True
            )

            hourly_values = [point.value_kwh for point in hourly_points]
            latest_hour_kwh = hourly_values[-1] if hourly_values else 0.0

            today_iso = date.today().isoformat()
            today_points = [p for p in hourly_points if p.date_time.startswith(today_iso)]
            today_values = [p.value_kwh for p in today_points]

            today_total_kwh = round(sum(today_values), 3)
            today_peak_hour_kwh = max(today_values) if today_values else 0.0

            if today_points:
                peak_point = max(today_points, key=lambda p: p.value_kwh)
                today_peak_hour_time = peak_point.date_time
            else:
                today_peak_hour_time = None

            return {
                "latest_day_kwh": latest_day_kwh,
                "month_to_date_kwh": month_to_date_kwh,
                "average_daily_kwh": avg_daily_kwh,
                "latest_hour_kwh": latest_hour_kwh,
                "today_total_kwh": today_total_kwh,
                "today_peak_hour_kwh": round(today_peak_hour_kwh, 3),
                "today_peak_hour_time": today_peak_hour_time,
                ATTR_START_DATE: month_start.isoformat(),
                ATTR_END_DATE: end_date.isoformat(),
                ATTR_POINTS: [asdict(point) for point in daily_points],
                ATTR_HOURLY_START_DATE: hourly_start.isoformat(),
                ATTR_HOURLY_END_DATE: hourly_end.isoformat(),
                ATTR_HOURLY_POINTS: [asdict(point) for point in hourly_points],
            }
        except VattenfallApiError as err:
            raise UpdateFailed(f"Failed to fetch data from Vattenfall API: {err}") from err
