"""The Cooling Zone Manager integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_MAX_ZONES,
    CONF_OVERLAP,
    CONF_ZONES,
    DEFAULT_MAX_ZONES,
    DEFAULT_OVERLAP,
    DOMAIN,
)
from .manager import CoolingZoneManager, Zone

PLATFORMS = [Platform.NUMBER, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Cooling Zone Manager from a config entry."""
    zones = [Zone(**zone) for zone in entry.data[CONF_ZONES]]
    manager = CoolingZoneManager(
        hass,
        entry.entry_id,
        zones,
        int(entry.data.get(CONF_MAX_ZONES, DEFAULT_MAX_ZONES)),
        int(entry.data.get(CONF_OVERLAP, DEFAULT_OVERLAP)),
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    # Set up entities first (the number entities restore their last values
    # and push them into the manager), then start reconciling.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await manager.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        manager: CoolingZoneManager = hass.data[DOMAIN].pop(entry.entry_id)
        await manager.async_stop()
    return unload_ok
