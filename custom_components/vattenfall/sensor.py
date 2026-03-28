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
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
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
        return {
            "start_date": self.coordinator.data.get("start_date"),
            "end_date": self.coordinator.data.get("end_date"),
            "points": self.coordinator.data.get("points", []),
        }
