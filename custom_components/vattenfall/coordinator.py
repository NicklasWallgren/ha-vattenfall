"""Data update coordinator for Vattenfall."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any
import zoneinfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ConsumptionPoint,
    HourlyConsumptionPoint,
    HourlyTemperaturePoint,
    VattenfallApiClient,
    VattenfallApiError,
)
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
    CONF_METERING_POINT_ID,
    CONF_TEMPERATURE_AREA_CODE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TEMPERATURE_AREA_CODE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


_API_TIMEZONE = zoneinfo.ZoneInfo("Europe/Stockholm")
_CHUNK_MONTHS = 3


def _date_range_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split [start, end] into chunks of up to _CHUNK_MONTHS months each."""
    chunks: list[tuple[date, date]] = []
    chunk_start = start
    while chunk_start <= end:
        m = chunk_start.month - 1 + _CHUNK_MONTHS
        next_start = date(chunk_start.year + m // 12, m % 12 + 1, 1)
        chunk_end = min(next_start - timedelta(days=1), end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = next_start
    return chunks


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
        mode: str = "all",
    ) -> None:
        """Backfill historical data into the HA recorder for a date range."""
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        if end_date >= date.today():
            raise ValueError("end_date must be before today; cannot backfill future data")

        chunks = _date_range_chunks(start_date, end_date)

        daily_points: list[ConsumptionPoint] = []
        hourly_points: list[HourlyConsumptionPoint] = []
        temperature_points: list[HourlyTemperaturePoint] = []

        if mode in ("daily", "all"):
            for chunk_start, chunk_end in chunks:
                chunk = await self.client.async_get_daily_consumption(chunk_start, chunk_end)
                daily_points.extend(chunk)
                _LOGGER.debug("Fetched daily chunk %s–%s (%d points)", chunk_start, chunk_end, len(chunk))

        if mode in ("hourly", "all"):
            for chunk_start, chunk_end in chunks:
                chunk = await self.client.async_get_hourly_consumption(chunk_start, chunk_end, include_load=True)
                hourly_points.extend(chunk)
                _LOGGER.debug("Fetched hourly chunk %s–%s (%d points)", chunk_start, chunk_end, len(chunk))

        if mode in ("temperature", "all"):
            for chunk_start, chunk_end in chunks:
                chunk = await self.client.async_get_hourly_temperature(chunk_start, chunk_end, use_cet=True)
                temperature_points.extend(chunk)
                _LOGGER.debug("Fetched temperature chunk %s–%s (%d points)", chunk_start, chunk_end, len(chunk))

        if daily_points or hourly_points or temperature_points:
            await self._async_write_statistics(daily_points, hourly_points, temperature_points)

        # Restore sensors to current data after backfill
        await self.async_request_refresh()

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

    async def _async_write_statistics(
        self,
        daily_points: list[ConsumptionPoint],
        hourly_points: list[HourlyConsumptionPoint],
        temperature_points: list[HourlyTemperaturePoint],
    ) -> None:
        """Write fetched data points as external statistics into the HA recorder."""
        # Lazy imports to avoid loading recorder at module level (breaks test stubs)
        from homeassistant.components.recorder.models import StatisticData, StatisticMetaData  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import async_add_external_statistics  # noqa: PLC0415
        from homeassistant.const import UnitOfEnergy, UnitOfTemperature  # noqa: PLC0415

        metering_point_id: str = self.entry.data[CONF_METERING_POINT_ID]
        statistic_prefix = metering_point_id.lower()

        if daily_points:
            statistic_id = f"{DOMAIN}:daily_consumption_{statistic_prefix}"
            first_day = datetime.fromisoformat(daily_points[0].date).date()
            range_start_dt = datetime(first_day.year, first_day.month, first_day.day, tzinfo=_API_TIMEZONE)
            last_sum = await self._async_last_sum_before(statistic_id, range_start_dt, "day")

            stats: list[StatisticData] = []
            cumsum = last_sum
            for point in daily_points:
                d = datetime.fromisoformat(point.date).date()
                start_dt = datetime(d.year, d.month, d.day, tzinfo=_API_TIMEZONE)
                cumsum = round(cumsum + point.value_kwh, 3)
                stats.append(StatisticData(start=start_dt, state=point.value_kwh, sum=cumsum))

            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"Vattenfall Daily Consumption {metering_point_id}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug("Wrote %d daily statistics for %s", len(stats), statistic_id)

        if hourly_points:
            statistic_id = f"{DOMAIN}:hourly_consumption_{statistic_prefix}"
            first_hour_dt = datetime.fromisoformat(hourly_points[0].date_time).replace(tzinfo=_API_TIMEZONE)
            last_sum = await self._async_last_sum_before(statistic_id, first_hour_dt, "hour")

            stats = []
            cumsum = last_sum
            for point in hourly_points:
                start_dt = datetime.fromisoformat(point.date_time).replace(tzinfo=_API_TIMEZONE)
                cumsum = round(cumsum + point.value_kwh, 3)
                stats.append(StatisticData(start=start_dt, state=point.value_kwh, sum=cumsum))

            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"Vattenfall Hourly Consumption {metering_point_id}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug("Wrote %d hourly statistics for %s", len(stats), statistic_id)

        if temperature_points:
            temperature_area_code: str = str(self.entry.data.get(
                CONF_TEMPERATURE_AREA_CODE, DEFAULT_TEMPERATURE_AREA_CODE
            )).lower()
            statistic_id = f"{DOMAIN}:temperature_{temperature_area_code}"

            stats = []
            for point in temperature_points:
                start_dt = datetime.fromisoformat(point.date_time).replace(tzinfo=_API_TIMEZONE)
                stats.append(StatisticData(start=start_dt, mean=point.value_c))

            metadata = StatisticMetaData(
                has_mean=True,
                has_sum=False,
                name=f"Vattenfall Outdoor Temperature {temperature_area_code}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=UnitOfTemperature.CELSIUS,
            )
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug("Wrote %d temperature statistics for %s", len(stats), statistic_id)

    async def _async_last_sum_before(
        self, statistic_id: str, before_dt: datetime, period: str
    ) -> float:
        """Return the cumulative sum of the last stat before before_dt for statistic_id, or 0."""
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        delta = timedelta(days=1) if period == "day" else timedelta(hours=1)
        existing = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            before_dt - delta,
            before_dt,
            {statistic_id},
            period,
            None,
            {"sum"},
        )
        rows = (existing or {}).get(statistic_id, [])
        return rows[-1].get("sum") or 0.0 if rows else 0.0
