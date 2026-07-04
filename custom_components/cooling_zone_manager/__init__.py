"""The Cooling Zone Manager integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration

from .const import (
    CONF_MAX_RUN,
    CONF_MAX_RUN_MINUTES,
    CONF_MAX_ZONES,
    CONF_OVERLAP,
    CONF_TEMP_SENSOR,
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
    if CONF_MAX_RUN_MINUTES in entry.data:
        max_run_seconds = int(entry.data[CONF_MAX_RUN_MINUTES]) * 60
    else:
        # Entries created by 1.1.0 stored this in seconds.
        max_run_seconds = int(entry.data.get(CONF_MAX_RUN, 0))
    manager = CoolingZoneManager(
        hass,
        entry.entry_id,
        zones,
        int(entry.data.get(CONF_MAX_ZONES, DEFAULT_MAX_ZONES)),
        int(entry.data.get(CONF_OVERLAP, DEFAULT_OVERLAP)),
        max_run_seconds,
        entry.data.get(CONF_TEMP_SENSOR),
    )
    integration = await async_get_integration(hass, DOMAIN)
    manager.version = str(integration.version)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    # Drop registry entries for zones/thresholds that no longer exist
    # (the options flow can remove zones or the temperature sensor).
    _async_cleanup_orphans(hass, entry, manager)

    # Set up entities first (the number entities restore their last values
    # and push them into the manager), then start reconciling.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await manager.async_start()

    # Reload when the options flow changes the entry data.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply options-flow changes by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_cleanup_orphans(
    hass: HomeAssistant, entry: ConfigEntry, manager: CoolingZoneManager
) -> None:
    """Remove registry entities that no current zone or setting provides."""
    registry = er.async_get(hass)
    expected = {
        f"{entry.entry_id}_status",
        f"{entry.entry_id}_total_runtime",
        f"{entry.entry_id}_max_zones",
        f"{entry.entry_id}_overlap",
        f"{entry.entry_id}_max_run",
    }
    for zone in manager.zones:
        expected.add(f"{entry.entry_id}_zone_{zone.name}_status")
        expected.add(f"{entry.entry_id}_zone_{zone.name}_runtime")
    if manager.temp_entity:
        for tier in range(2, len(manager.zones) + 1):
            expected.add(f"{entry.entry_id}_threshold_{tier}")
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id not in expected:
            registry.async_remove(entity.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        manager: CoolingZoneManager = hass.data[DOMAIN].pop(entry.entry_id)
        await manager.async_stop()
    return unload_ok
