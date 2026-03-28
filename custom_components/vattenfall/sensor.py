"""Sensor platform for Vattenfall."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

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
    DOMAIN,
)
from .coordinator import VattenfallDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class VattenfallSensorEntityDescription(SensorEntityDescription):
    """Describes Vattenfall sensor entity."""

    value_key: str


SENSORS: tuple[VattenfallSensorEntityDescription, ...] = (
    VattenfallSensorEntityDescription(
        key="latest_day",
        translation_key="latest_day",
        name="Latest Day Consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="latest_day_kwh",
        icon="mdi:lightning-bolt",
    ),
    VattenfallSensorEntityDescription(
        key="month_to_date",
        translation_key="month_to_date",
        name="Month To Date Consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="month_to_date_kwh",
        icon="mdi:calendar-month",
    ),
    VattenfallSensorEntityDescription(
        key="average_daily",
        translation_key="average_daily",
        name="Average Daily Consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="average_daily_kwh",
        icon="mdi:chart-line",
    ),
    VattenfallSensorEntityDescription(
        key="latest_hour",
        translation_key="latest_hour",
        name="Latest Hour Consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="latest_hour_kwh",
        icon="mdi:clock-outline",
    ),
    VattenfallSensorEntityDescription(
        key="today_total",
        translation_key="today_total",
        name="Today Total Consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="today_total_kwh",
        icon="mdi:calendar-today",
    ),
    VattenfallSensorEntityDescription(
        key="today_peak_hour",
        translation_key="today_peak_hour",
        name="Today Peak Hour Consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_peak_hour_kwh",
        icon="mdi:chart-bell-curve-cumulative",
    ),
    VattenfallSensorEntityDescription(
        key="latest_temperature",
        translation_key="latest_temperature",
        name="Latest Outdoor Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="latest_temperature_c",
        icon="mdi:thermometer",
    ),
    VattenfallSensorEntityDescription(
        key="today_avg_temperature",
        translation_key="today_avg_temperature",
        name="Today Average Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_avg_temperature_c",
        icon="mdi:thermometer-lines",
    ),
    VattenfallSensorEntityDescription(
        key="today_min_temperature",
        translation_key="today_min_temperature",
        name="Today Minimum Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_min_temperature_c",
        icon="mdi:thermometer-low",
    ),
    VattenfallSensorEntityDescription(
        key="today_max_temperature",
        translation_key="today_max_temperature",
        name="Today Maximum Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_max_temperature_c",
        icon="mdi:thermometer-high",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vattenfall sensors from config entry."""
    coordinator: VattenfallDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]

    entities = [
        VattenfallSensor(coordinator=coordinator, entry=entry, description=description)
        for description in SENSORS
    ]
    async_add_entities(entities)


class VattenfallSensor(CoordinatorEntity[VattenfallDataUpdateCoordinator], SensorEntity):
    """Representation of a Vattenfall sensor."""

    entity_description: VattenfallSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VattenfallDataUpdateCoordinator,
        entry: ConfigEntry,
        description: VattenfallSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    @property
    def native_value(self) -> float | None:
        """Return sensor value."""
        value: Any = self.coordinator.data.get(self.entity_description.value_key)
        if value is None:
            return None

        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        daily_points = self.coordinator.data.get(ATTR_POINTS, [])
        hourly_points = self.coordinator.data.get(ATTR_HOURLY_POINTS, [])
        temperature_points = self.coordinator.data.get(ATTR_TEMPERATURE_POINTS, [])
        return {
            "start_date": self.coordinator.data.get(ATTR_START_DATE),
            "end_date": self.coordinator.data.get(ATTR_END_DATE),
            "daily_points_count": len(daily_points),
            "hourly_start_date": self.coordinator.data.get(ATTR_HOURLY_START_DATE),
            "hourly_end_date": self.coordinator.data.get(ATTR_HOURLY_END_DATE),
            "hourly_points_count": len(hourly_points),
            "temperature_start_date": self.coordinator.data.get(ATTR_TEMPERATURE_START_DATE),
            "temperature_end_date": self.coordinator.data.get(ATTR_TEMPERATURE_END_DATE),
            "temperature_points_count": len(temperature_points),
            "today_peak_hour_time": self.coordinator.data.get("today_peak_hour_time"),
            "backfill_mode": self.coordinator.data.get("backfill_mode"),
            "backfill_start_date": self.coordinator.data.get("backfill_start_date"),
            "backfill_end_date": self.coordinator.data.get("backfill_end_date"),
        }
