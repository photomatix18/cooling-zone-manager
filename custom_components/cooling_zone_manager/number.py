"""Number entities exposing the manager's tunables (max zones, overlap)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
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
    """Set up the number entities."""
    manager: CoolingZoneManager = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = [
        MaxZonesNumber(manager, entry),
        OverlapNumber(manager, entry),
        MaxRunNumber(manager, entry),
    ]
    # Temperature-aware capacity: one threshold per zone beyond the first,
    # only when an outdoor temperature sensor was configured.
    if manager.temp_entity:
        for tier in range(2, len(manager.zones) + 1):
            entities.append(ZoneThresholdNumber(manager, entry, tier))
    async_add_entities(entities)


class _ManagerNumber(RestoreNumber):
    """Base for numbers that push their value into the manager."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_should_poll = False

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Cooling Zone Manager",
            model="Zone arbiter",
            sw_version=manager.version,
        )

    def _push(self, value: float) -> None:
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        """Restore the last value and push it into the manager."""
        await super().async_added_to_hass()
        data = await self.async_get_last_number_data()
        if data is not None and data.native_value is not None:
            value = self._convert_restored(data)
            self._attr_native_value = value
            self._push(value)

    def _convert_restored(self, data) -> float:
        """Hook for entities whose unit changed between versions."""
        return data.native_value

    async def async_set_native_value(self, value: float) -> None:
        """Handle a change from the UI."""
        self._attr_native_value = value
        self._push(value)
        self.async_write_ha_state()
        await self._manager.async_reconcile()


class MaxZonesNumber(_ManagerNumber):
    """How many zones may cool at the same time."""

    _attr_name = "Max zones"
    _attr_icon = "mdi:counter"
    _attr_native_min_value = 1
    _attr_native_step = 1

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_max_zones"
        self._attr_native_max_value = max(len(manager.zones), 1)
        self._attr_native_value = manager.max_zones

    def _push(self, value: float) -> None:
        self._manager.max_zones = int(value)


class OverlapNumber(_ManagerNumber):
    """How long a zone keeps running after its request drops."""

    _attr_name = "Overlap time"
    _attr_icon = "mdi:timer-outline"
    _attr_native_min_value = 0
    _attr_native_max_value = 3600
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_overlap"
        self._attr_native_value = manager.overlap

    def _push(self, value: float) -> None:
        self._manager.overlap = int(value)


class MaxRunNumber(_ManagerNumber):
    """Longest a zone may cool while other zones wait. 0 disables it.

    Set in minutes; the manager keeps seconds internally.
    """

    _attr_name = "Max zone run time"
    _attr_icon = "mdi:timer-alert-outline"
    _attr_native_min_value = 0
    _attr_native_max_value = 480
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_max_run"
        self._attr_native_value = manager.max_run // 60

    def _convert_restored(self, data) -> float:
        # 1.1.0 stored this value in seconds.
        if data.native_unit_of_measurement == UnitOfTime.SECONDS:
            return round(data.native_value / 60)
        return data.native_value

    def _push(self, value: float) -> None:
        self._manager.max_run = int(value) * 60


class ZoneThresholdNumber(_ManagerNumber):
    """Outdoor temperature below which ``tier`` zones may run at once."""

    _attr_icon = "mdi:thermometer-chevron-down"
    _attr_native_step = 0.5

    def __init__(
        self, manager: CoolingZoneManager, entry: ConfigEntry, tier: int
    ) -> None:
        super().__init__(manager, entry)
        self._tier = tier
        self._attr_unique_id = f"{entry.entry_id}_threshold_{tier}"
        self._attr_name = f"Allow {tier} zones below"
        unit = manager.hass.config.units.temperature_unit
        self._attr_native_unit_of_measurement = unit
        if unit == UnitOfTemperature.CELSIUS:
            self._attr_native_min_value = -20
            self._attr_native_max_value = 55
            default = 29.0 - 5.0 * (tier - 2)
        else:
            self._attr_native_min_value = 0
            self._attr_native_max_value = 130
            default = 85.0 - 10.0 * (tier - 2)
        self._attr_native_value = default
        self._push(default)

    def _push(self, value: float) -> None:
        self._manager.zone_thresholds[self._tier] = float(value)
