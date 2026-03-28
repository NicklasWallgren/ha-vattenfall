"""Data update coordinator for Vattenfall."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import VattenfallApiClient, VattenfallApiError
from .const import ATTR_END_DATE, ATTR_POINTS, ATTR_START_DATE, DEFAULT_SCAN_INTERVAL

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
            today = date.today()
            month_start = today.replace(day=1)

            points = await self.client.async_get_daily_consumption(month_start, today)
            values = [point.value_kwh for point in points]

            latest_day_kwh = values[-1] if values else 0.0
            month_to_date_kwh = round(sum(values), 3)
            avg_daily_kwh = round(month_to_date_kwh / len(values), 3) if values else 0.0

            return {
                "latest_day_kwh": latest_day_kwh,
                "month_to_date_kwh": month_to_date_kwh,
                "average_daily_kwh": avg_daily_kwh,
                ATTR_START_DATE: month_start.isoformat(),
                ATTR_END_DATE: today.isoformat(),
                ATTR_POINTS: [asdict(point) for point in points],
            }
        except VattenfallApiError as err:
            raise UpdateFailed(f"Failed to fetch data from Vattenfall API: {err}") from err
