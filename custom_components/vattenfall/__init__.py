"""The Vattenfall integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import VattenfallApiClient
from .const import DOMAIN
from .coordinator import VattenfallDataUpdateCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vattenfall from a config entry."""
    client = VattenfallApiClient(hass=hass, config=entry.data)
    coordinator = VattenfallDataUpdateCoordinator(hass=hass, client=client, entry=entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }

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

    return unload_ok
