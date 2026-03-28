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
from homeassistant.helpers.device_registry import DeviceInfo
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
    attribute_group: str = "daily"  # "daily" | "hourly" | "temperature"


SENSORS: tuple[VattenfallSensorEntityDescription, ...] = (
    VattenfallSensorEntityDescription(
        key="latest_day",
        translation_key="latest_day",
        name="Latest day consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="latest_day_kwh",
        icon="mdi:lightning-bolt",
        attribute_group="daily",
    ),
    VattenfallSensorEntityDescription(
        key="month_to_date",
        translation_key="month_to_date",
        name="Month to date consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="month_to_date_kwh",
        icon="mdi:calendar-month",
        attribute_group="daily",
    ),
    VattenfallSensorEntityDescription(
        key="average_daily",
        translation_key="average_daily",
        name="Average daily consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="average_daily_kwh",
        icon="mdi:chart-line",
        attribute_group="daily",
    ),
    VattenfallSensorEntityDescription(
        key="latest_hour",
        translation_key="latest_hour",
        name="Latest hour consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="latest_hour_kwh",
        icon="mdi:clock-outline",
        attribute_group="hourly",
    ),
    VattenfallSensorEntityDescription(
        key="today_total",
        translation_key="today_total",
        name="Latest day hourly total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="today_total_kwh",
        icon="mdi:calendar-today",
        attribute_group="hourly",
    ),
    VattenfallSensorEntityDescription(
        key="today_peak_hour",
        translation_key="today_peak_hour",
        name="Latest day peak hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_peak_hour_kwh",
        icon="mdi:chart-bell-curve-cumulative",
        attribute_group="hourly",
    ),
    VattenfallSensorEntityDescription(
        key="latest_temperature",
        translation_key="latest_temperature",
        name="Latest outdoor temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="latest_temperature_c",
        icon="mdi:thermometer",
        attribute_group="temperature",
    ),
    VattenfallSensorEntityDescription(
        key="today_avg_temperature",
        translation_key="today_avg_temperature",
        name="Latest day average temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_avg_temperature_c",
        icon="mdi:thermometer-lines",
        attribute_group="temperature",
    ),
    VattenfallSensorEntityDescription(
        key="today_min_temperature",
        translation_key="today_min_temperature",
        name="Latest day minimum temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_min_temperature_c",
        icon="mdi:thermometer-low",
        attribute_group="temperature",
    ),
    VattenfallSensorEntityDescription(
        key="today_max_temperature",
        translation_key="today_max_temperature",
        name="Latest day maximum temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_max_temperature_c",
        icon="mdi:thermometer-high",
        attribute_group="temperature",
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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Vattenfall",
            manufacturer="Vattenfall",
        )

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
        """Return state attributes scoped to this sensor's data group."""
        data = self.coordinator.data
        group = self.entity_description.attribute_group

        if group == "daily":
            return {
                "start_date": data.get(ATTR_START_DATE),
                "end_date": data.get(ATTR_END_DATE),
                "points_count": len(data.get(ATTR_POINTS, [])),
            }
        if group == "hourly":
            return {
                "start_date": data.get(ATTR_HOURLY_START_DATE),
                "end_date": data.get(ATTR_HOURLY_END_DATE),
                "points_count": len(data.get(ATTR_HOURLY_POINTS, [])),
                "peak_hour_time": data.get("today_peak_hour_time"),
            }
        if group == "temperature":
            return {
                "start_date": data.get(ATTR_TEMPERATURE_START_DATE),
                "end_date": data.get(ATTR_TEMPERATURE_END_DATE),
                "points_count": len(data.get(ATTR_TEMPERATURE_POINTS, [])),
            }
        return {}
