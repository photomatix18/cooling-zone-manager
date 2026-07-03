"""Status sensor for the Cooling Zone Manager."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .manager import CoolingZoneManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the status sensor."""
    manager: CoolingZoneManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ZoneStatusSensor(manager, entry)])


class ZoneStatusSensor(SensorEntity):
    """Shows how many zones are cooling, with full detail as attributes."""

    _attr_has_entity_name = True
    _attr_name = "Active zones"
    _attr_icon = "mdi:hvac"
    _attr_should_poll = False
    _attr_native_unit_of_measurement = "zones"

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Cooling Zone Manager",
            model="Zone arbiter",
        )

    async def async_added_to_hass(self) -> None:
        """Update whenever the manager reconciles."""
        self.async_on_remove(self._manager.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> int:
        return len(self._manager.active_zones)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        manager = self._manager
        return {
            "active_zones": manager.active_zones,
            "requesting": manager.requesting_zones,
            "winding_down": manager.winding_down,
            "waiting": manager.waiting_zones,
            "round_robin_order": manager.rr_order,
            "max_zones": manager.max_zones,
            "overlap_seconds": manager.overlap,
        }
