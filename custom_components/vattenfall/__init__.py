"""The Vattenfall integration."""

from __future__ import annotations

from datetime import date
import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .api import VattenfallApiClient
from .const import (
    DOMAIN,
    SERVICE_ATTR_END_DATE,
    SERVICE_ATTR_ENTRY_ID,
    SERVICE_ATTR_MODE,
    SERVICE_ATTR_START_DATE,
    SERVICE_BACKFILL,
)
from .coordinator import VattenfallDataUpdateCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)

BACKFILL_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(SERVICE_ATTR_START_DATE): cv.date,
        vol.Required(SERVICE_ATTR_END_DATE): cv.date,
        vol.Optional(SERVICE_ATTR_MODE, default="all"): vol.In(
            ["daily", "hourly", "temperature", "all"]
        ),
        vol.Optional(SERVICE_ATTR_ENTRY_ID): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vattenfall from a config entry."""
    client = VattenfallApiClient(hass=hass, config=entry.data)
    coordinator = VattenfallDataUpdateCoordinator(hass=hass, client=client, entry=entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL):
        async def _handle_backfill(call: ServiceCall) -> None:
            """Async wrapper so Home Assistant awaits the backfill coroutine."""
            await _async_handle_backfill_service(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_BACKFILL,
            _handle_backfill,
            schema=BACKFILL_SERVICE_SCHEMA,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        runtime_data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if runtime_data is not None:
            client: VattenfallApiClient = runtime_data["client"]
            await client.async_close()

        if not hass.data[DOMAIN] and hass.services.has_service(DOMAIN, SERVICE_BACKFILL):
            hass.services.async_remove(DOMAIN, SERVICE_BACKFILL)

    return unload_ok


async def _async_handle_backfill_service(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle service call to backfill historical data ranges."""
    start_date = call.data[SERVICE_ATTR_START_DATE]
    end_date = call.data[SERVICE_ATTR_END_DATE]
    mode = call.data[SERVICE_ATTR_MODE]
    entry_id = call.data.get(SERVICE_ATTR_ENTRY_ID)

    if end_date < start_date:
        raise HomeAssistantError("end_date must be on or after start_date")
    if end_date >= date.today():
        raise HomeAssistantError("end_date must be before today; cannot backfill future data")

    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data:
        raise HomeAssistantError("No Vattenfall config entries loaded")

    targets: list[tuple[str, dict]] = []
    if entry_id is not None:
        target = domain_data.get(entry_id)
        if target is None:
            raise HomeAssistantError(f"No loaded Vattenfall entry found for entry_id={entry_id}")
        targets.append((entry_id, target))
    else:
        targets = list(domain_data.items())

    failures: list[str] = []
    for target_entry_id, runtime_data in targets:
        coordinator: VattenfallDataUpdateCoordinator = runtime_data["coordinator"]
        try:
            await coordinator.async_backfill_range(start_date, end_date, mode=mode)
            _LOGGER.info(
                "Completed Vattenfall backfill for entry_id=%s mode=%s range=%s..%s",
                target_entry_id,
                mode,
                start_date,
                end_date,
            )
        except Exception as err:  # pylint: disable=broad-except
            failures.append(f"{target_entry_id}: {err}")

    if failures:
        raise HomeAssistantError(f"Backfill failed for one or more entries: {', '.join(failures)}")
