"""Data update coordinator for Vattenfall."""

from __future__ import annotations

import logging
import zoneinfo
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ConsumptionPoint,
    HourlyConsumptionPoint,
    HourlyTemperaturePoint,
    VattenfallAccessDeniedError,
    VattenfallApiClient,
    VattenfallApiError,
)
from .const import (
    ATTR_END_DATE,
    ATTR_FAILED_GROUPS,
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

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import StatisticData

_LOGGER = logging.getLogger(__name__)


_API_TIMEZONE = zoneinfo.ZoneInfo("Europe/Stockholm")
_CHUNK_MONTHS = 3
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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


def _day_start(value: str) -> datetime:
    """Return local midnight for an API date or datetime string."""
    day = datetime.fromisoformat(value).date()
    return datetime(day.year, day.month, day.day, tzinfo=_API_TIMEZONE)


def _hours_in_day(day: str) -> int:
    """Return the length of a local day in hours, which is 23 or 25 on DST changes."""
    start = _day_start(day)
    end = _day_start((start.date() + timedelta(days=1)).isoformat())
    return round((end.timestamp() - start.timestamp()) / 3600)


def _hourly_timestamps(date_times: list[str]) -> list[float]:
    """Convert API local times to UTC timestamps, keeping the repeated DST hour apart."""
    timestamps: list[float] = []
    previous: datetime | None = None
    for value in date_times:
        local = datetime.fromisoformat(value)
        fold = 1 if local == previous else 0
        timestamps.append(local.replace(tzinfo=_API_TIMEZONE, fold=fold).timestamp())
        previous = local
    return timestamps


def _latest_day(date_times: list[str], before: date) -> str | None:
    """Return the newest day before the given date with hourly data, preferring full days."""
    counts = Counter(value[:10] for value in date_times)
    days = [day for day in counts if day < before.isoformat()]
    full_days = [day for day in days if counts[day] >= _hours_in_day(day)]
    return max(full_days or days, default=None)


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
            # Vattenfall publishes about once a day, so yesterday may still be missing.
            hourly_start = hourly_end - timedelta(days=2)
            data = await self._async_build_data(
                daily_start=month_start,
                daily_end=end_date,
                hourly_start=hourly_start,
                hourly_end=hourly_end,
                temperature_start=hourly_start,
                temperature_end=hourly_end,
            )
            data.update(await self._async_fetch_contract_data())
            return data
        except VattenfallAccessDeniedError as err:
            raise ConfigEntryAuthFailed(
                f"Vattenfall rejected the login: {err}"
            ) from err
        except VattenfallApiError as err:
            raise UpdateFailed(
                f"Failed to fetch data from Vattenfall API: {err}"
            ) from err

    async def _async_fetch_contract_data(self) -> dict[str, Any]:
        """Fetch contract tariff/fee details and expose them as data keys."""
        tariff = await self.client.async_get_contract()
        return {
            "transfer_fee_incl_vat": tariff.transfer_fee_incl_vat,
            "energy_tax_incl_vat": tariff.energy_tax_incl_vat,
            "fixed_fee_incl_vat_incl_discount": tariff.fixed_fee_incl_vat_incl_discount,
        }

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
            raise ValueError(
                "end_date must be before today; cannot backfill future data"
            )

        chunks = _date_range_chunks(start_date, end_date)

        daily_points: list[ConsumptionPoint] = []
        hourly_points: list[HourlyConsumptionPoint] = []
        temperature_points: list[HourlyTemperaturePoint] = []

        if mode in ("daily", "all"):
            for chunk_start, chunk_end in chunks:
                chunk = await self.client.async_get_daily_consumption(
                    chunk_start, chunk_end, allow_stub=False
                )
                daily_points.extend(chunk)
                _LOGGER.debug(
                    "Fetched daily chunk %s–%s (%d points)",
                    chunk_start,
                    chunk_end,
                    len(chunk),
                )

        if mode in ("hourly", "all"):
            for chunk_start, chunk_end in chunks:
                chunk = await self.client.async_get_hourly_consumption(
                    chunk_start, chunk_end, include_load=True, allow_stub=False
                )
                hourly_points.extend(chunk)
                _LOGGER.debug(
                    "Fetched hourly chunk %s–%s (%d points)",
                    chunk_start,
                    chunk_end,
                    len(chunk),
                )

        if mode in ("temperature", "all"):
            for chunk_start, chunk_end in chunks:
                chunk = await self.client.async_get_hourly_temperature(
                    chunk_start, chunk_end, use_cet=True, allow_stub=False
                )
                temperature_points.extend(chunk)
                _LOGGER.debug(
                    "Fetched temperature chunk %s–%s (%d points)",
                    chunk_start,
                    chunk_end,
                    len(chunk),
                )

        if daily_points or hourly_points or temperature_points:
            await self._async_write_statistics(
                daily_points, hourly_points, temperature_points
            )

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
        """Build coordinator data, letting each data group fail on its own."""
        builders: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {}
        if daily_start is not None and daily_end is not None:
            builders["daily"] = lambda: self._async_build_daily(daily_start, daily_end)
        if hourly_start is not None and hourly_end is not None:
            builders["hourly"] = lambda: self._async_build_hourly(
                hourly_start, hourly_end
            )
        if temperature_start is not None and temperature_end is not None:
            builders["temperature"] = lambda: self._async_build_temperature(
                temperature_start, temperature_end
            )

        previously_failed = (self.data or {}).get(ATTR_FAILED_GROUPS, set())
        data: dict[str, Any] = {}
        errors: dict[str, VattenfallApiError] = {}
        for group, build in builders.items():
            try:
                data.update(await build())
            except VattenfallAccessDeniedError:
                raise
            except VattenfallApiError as err:
                errors[group] = err
                if group not in previously_failed:
                    _LOGGER.warning(
                        "Could not fetch Vattenfall %s data: %s", group, err
                    )
            else:
                if group in previously_failed:
                    _LOGGER.info("Vattenfall %s data is available again", group)

        if errors and len(errors) == len(builders):
            raise next(iter(errors.values()))

        data[ATTR_FAILED_GROUPS] = set(errors)
        return data

    async def _async_build_daily(
        self, daily_start: date, daily_end: date
    ) -> dict[str, Any]:
        """Build the daily consumption part of the coordinator data."""
        daily_points = await self.client.async_get_daily_consumption(
            daily_start, daily_end
        )
        daily_values = [point.value_kwh for point in daily_points]

        latest_day_kwh = daily_values[-1] if daily_values else 0.0
        month_to_date_kwh = round(sum(daily_values), 3)
        avg_daily_kwh = (
            round(month_to_date_kwh / len(daily_values), 3) if daily_values else 0.0
        )

        return {
            "latest_day_kwh": latest_day_kwh,
            "latest_day_reset": _day_start(daily_points[-1].date)
            if daily_points
            else None,
            "month_to_date_reset": _day_start(daily_start.isoformat()),
            "month_to_date_kwh": month_to_date_kwh,
            "average_daily_kwh": avg_daily_kwh,
            ATTR_START_DATE: daily_start.isoformat(),
            ATTR_END_DATE: daily_end.isoformat(),
            ATTR_POINTS: [asdict(point) for point in daily_points],
        }

    async def _async_build_hourly(
        self, hourly_start: date, hourly_end: date
    ) -> dict[str, Any]:
        """Build the hourly consumption part of the coordinator data."""
        hourly_points = await self.client.async_get_hourly_consumption(
            hourly_start, hourly_end, include_load=True
        )
        hourly_values = [point.value_kwh for point in hourly_points]
        latest_hour_kwh = hourly_values[-1] if hourly_values else 0.0

        latest_day = _latest_day(
            [p.date_time for p in hourly_points], before=hourly_end
        )
        latest_day_points = [
            p
            for p in hourly_points
            if latest_day and p.date_time.startswith(latest_day)
        ]
        latest_day_values = [p.value_kwh for p in latest_day_points]

        latest_day_total_kwh = (
            round(sum(latest_day_values), 3) if latest_day_values else None
        )
        latest_day_peak_hour_kwh = (
            round(max(latest_day_values), 3) if latest_day_values else None
        )
        if latest_day_points:
            peak_point = max(latest_day_points, key=lambda p: p.value_kwh)
            latest_day_peak_hour_time = peak_point.date_time
        else:
            latest_day_peak_hour_time = None

        return {
            "latest_hour_kwh": latest_hour_kwh,
            "latest_reading": (
                datetime.fromtimestamp(
                    _hourly_timestamps([p.date_time for p in hourly_points])[-1],
                    timezone.utc,
                )
                if hourly_points
                else None
            ),
            "today_total_kwh": latest_day_total_kwh,
            "today_peak_hour_kwh": latest_day_peak_hour_kwh,
            "today_peak_hour_time": latest_day_peak_hour_time,
            "hourly_latest_day": latest_day,
            "hourly_latest_day_reset": _day_start(latest_day) if latest_day else None,
            ATTR_HOURLY_START_DATE: hourly_start.isoformat(),
            ATTR_HOURLY_END_DATE: hourly_end.isoformat(),
            ATTR_HOURLY_POINTS: [asdict(point) for point in hourly_points],
        }

    async def _async_build_temperature(
        self, temperature_start: date, temperature_end: date
    ) -> dict[str, Any]:
        """Build the temperature part of the coordinator data."""
        temperature_points = await self.client.async_get_hourly_temperature(
            temperature_start, temperature_end, use_cet=True
        )
        latest_temp_day = _latest_day(
            [p.date_time for p in temperature_points], before=temperature_end
        )
        selected_day_temp_values = [
            p.value_c
            for p in temperature_points
            if latest_temp_day and p.date_time.startswith(latest_temp_day)
        ]

        latest_temperature_c = (
            temperature_points[-1].value_c if temperature_points else None
        )
        today_avg_temperature_c = (
            round(sum(selected_day_temp_values) / len(selected_day_temp_values), 2)
            if selected_day_temp_values
            else None
        )
        today_min_temperature_c = (
            round(min(selected_day_temp_values), 2)
            if selected_day_temp_values
            else None
        )
        today_max_temperature_c = (
            round(max(selected_day_temp_values), 2)
            if selected_day_temp_values
            else None
        )

        return {
            "latest_temperature_c": latest_temperature_c,
            "today_avg_temperature_c": today_avg_temperature_c,
            "today_min_temperature_c": today_min_temperature_c,
            "today_max_temperature_c": today_max_temperature_c,
            "temperature_latest_day": latest_temp_day,
            ATTR_TEMPERATURE_START_DATE: temperature_start.isoformat(),
            ATTR_TEMPERATURE_END_DATE: temperature_end.isoformat(),
            ATTR_TEMPERATURE_POINTS: [asdict(point) for point in temperature_points],
        }

    async def _async_write_statistics(
        self,
        daily_points: list[ConsumptionPoint],
        hourly_points: list[HourlyConsumptionPoint],
        temperature_points: list[HourlyTemperaturePoint],
    ) -> None:
        """Write fetched data points as external statistics into the HA recorder."""
        # Lazy imports to avoid loading recorder at module level (breaks test stubs)
        from homeassistant.components.recorder.models import (  # noqa: PLC0415
            StatisticData,
            StatisticMeanType,
            StatisticMetaData,
        )
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,  # noqa: PLC0415
        )
        from homeassistant.const import UnitOfEnergy, UnitOfTemperature  # noqa: PLC0415
        from homeassistant.util.unit_conversion import (  # noqa: PLC0415
            EnergyConverter,
            TemperatureConverter,
        )

        metering_point_id: str = self.entry.data[CONF_METERING_POINT_ID]
        statistic_prefix = metering_point_id.lower()

        if daily_points:
            statistic_id = f"{DOMAIN}:daily_consumption_{statistic_prefix}"
            stats = await self._async_merged_sum_statistics(
                statistic_id,
                {
                    _day_start(point.date).timestamp(): point.value_kwh
                    for point in daily_points
                },
            )

            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"Vattenfall Daily Consumption {metering_point_id}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug("Wrote %d daily statistics for %s", len(stats), statistic_id)

        if hourly_points:
            statistic_id = f"{DOMAIN}:hourly_consumption_{statistic_prefix}"
            timestamps = _hourly_timestamps(
                [point.date_time for point in hourly_points]
            )
            stats = await self._async_merged_sum_statistics(
                statistic_id,
                {ts: point.value_kwh for ts, point in zip(timestamps, hourly_points)},
            )

            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"Vattenfall Hourly Consumption {metering_point_id}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug("Wrote %d hourly statistics for %s", len(stats), statistic_id)

        if temperature_points:
            temperature_area_code: str = str(
                self.entry.data.get(
                    CONF_TEMPERATURE_AREA_CODE, DEFAULT_TEMPERATURE_AREA_CODE
                )
            ).lower()
            statistic_id = f"{DOMAIN}:temperature_{temperature_area_code}"

            timestamps = _hourly_timestamps(
                [point.date_time for point in temperature_points]
            )
            stats = [
                StatisticData(
                    start=datetime.fromtimestamp(ts, timezone.utc), mean=point.value_c
                )
                for ts, point in zip(timestamps, temperature_points)
            ]

            metadata = StatisticMetaData(
                has_mean=True,
                has_sum=False,
                mean_type=StatisticMeanType.ARITHMETIC,
                name=f"Vattenfall Outdoor Temperature {temperature_area_code}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=TemperatureConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfTemperature.CELSIUS,
            )
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug(
                "Wrote %d temperature statistics for %s", len(stats), statistic_id
            )

    async def _async_merged_sum_statistics(
        self, statistic_id: str, new_values: dict[float, float]
    ) -> list[StatisticData]:
        """Merge new_values over recorded rows and rebuild the running total.

        Recorded rows after the first new value are returned too, so the total stays
        continuous when a gap is filled or an older range is backfilled.
        """
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.models import StatisticData  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,  # noqa: PLC0415
        )

        existing = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            _EPOCH,
            None,
            {statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )

        first_start = min(new_values)
        cumsum = 0.0
        merged = dict(new_values)
        for row in (existing or {}).get(statistic_id, []):
            if row["start"] < first_start:
                cumsum = row.get("sum") or 0.0
            elif row.get("state") is not None:
                merged.setdefault(row["start"], row["state"])

        stats: list[StatisticData] = []
        for start in sorted(merged):
            cumsum = round(cumsum + merged[start], 3)
            stats.append(
                StatisticData(
                    start=datetime.fromtimestamp(start, timezone.utc),
                    state=merged[start],
                    sum=cumsum,
                )
            )
        return stats
