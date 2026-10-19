"""Number entities exposing the manager's tunables (max zones, overlap)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_CHANGEOVER_BAND_C,
    DEFAULT_CHANGEOVER_BAND_F,
    DEFAULT_CHANGEOVER_MINUTES,
    DEFAULT_COOLING_LOCKOUT_C,
    DEFAULT_COOLING_LOCKOUT_F,
    DEFAULT_HEATING_LOCKOUT_C,
    DEFAULT_HEATING_LOCKOUT_F,
    DEFAULT_PRIORITY,
    DOMAIN,
    MODE_HEATING,
    PRIORITY_MAX,
    PRIORITY_MIN,
)
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
        CooldownNumber(manager, entry),
        ChangeoverDelayNumber(manager, entry),
        ChangeoverBandNumber(manager, entry),
    ]
    if manager.mode == MODE_HEATING:
        entities.append(MinZonesNumber(manager, entry))
        entities.append(CompanionMarginNumber(manager, entry))
    for zone in manager.zones:
        entities.append(ZonePriorityNumber(manager, entry, zone.name))
    if manager.temp_entity:
        entities.append(LockoutNumber(manager, entry))
        # Temperature-aware capacity: one threshold per zone beyond the
        # first. Cooling only — heat output does not fall off in the cold.
        if manager.mode != MODE_HEATING:
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
            manufacturer="HVAC Zone Manager",
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


class CooldownNumber(_ManagerNumber):
    """Minimum compressor off time once every zone is off. 0 disables it.

    Set in minutes; the manager keeps seconds internally. Each entry has
    its own copy of this knob, so cooling and heating cooldowns are
    independent (set 0 wherever none is wanted).
    """

    _attr_icon = "mdi:timer-lock-outline"
    _attr_native_min_value = 0
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_cooldown"
        self._attr_name = (
            "Furnace cooldown" if manager.mode == MODE_HEATING else "Compressor cooldown"
        )
        self._attr_native_value = manager.cooldown // 60

    def _push(self, value: float) -> None:
        self._manager.cooldown = int(value) * 60


class MinZonesNumber(_ManagerNumber):
    """The floor: zones that run together whenever any zone runs.

    0 or 1 turns it off. Heating entries only.
    """

    _attr_name = "Min zones"
    _attr_icon = "mdi:counter"
    _attr_native_min_value = 0
    _attr_native_step = 1

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_min_zones"
        self._attr_native_max_value = max(len(manager.zones), 1)
        self._attr_native_value = manager.min_zones

    def _push(self, value: float) -> None:
        self._manager.min_zones = int(value)


class CompanionMarginNumber(_ManagerNumber):
    """How far past its own setpoint a companion zone may go.

    A companion runs for the equipment's sake, not because its room
    asked; once the room is this many degrees past setpoint it gives its
    slot back. Heating entries only.
    """

    _attr_name = "Companion overshoot"
    _attr_icon = "mdi:thermometer-chevron-up"
    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 0.5

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_companion_margin"
        self._attr_native_unit_of_measurement = (
            manager.hass.config.units.temperature_unit
        )
        self._attr_native_value = manager.companion_margin

    def _push(self, value: float) -> None:
        self._manager.companion_margin = float(value)


class ZonePriorityNumber(_ManagerNumber):
    """The zone's priority: 1 runs first, 3 yields to everyone.

    Meant to be driven by automations (bedtime, cooking, movie night) or
    a dashboard card; the manager just obeys the current values.
    """

    _attr_icon = "mdi:priority-high"
    _attr_native_min_value = PRIORITY_MIN
    _attr_native_max_value = PRIORITY_MAX
    _attr_native_step = 1

    def __init__(
        self, manager: CoolingZoneManager, entry: ConfigEntry, zone_name: str
    ) -> None:
        super().__init__(manager, entry)
        self._zone_name = zone_name
        self._attr_unique_id = f"{entry.entry_id}_priority_{zone_name}"
        self._attr_name = f"{zone_name} priority"
        self._attr_native_value = DEFAULT_PRIORITY
        self._push(DEFAULT_PRIORITY)

    def _push(self, value: float) -> None:
        self._manager.zone_priorities[self._zone_name] = int(value)


class LockoutNumber(_ManagerNumber):
    """Outdoor temperature past which this direction never runs.

    Cooling: no cooling below it (a compressor without a low-ambient kit
    ices its coil). Heating: no heating above it. Set it to the end of
    the range to effectively turn it off.
    """

    _attr_native_step = 0.5

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_lockout"
        heating = manager.mode == MODE_HEATING
        self._attr_name = "Heating lockout above" if heating else "Cooling lockout below"
        self._attr_icon = (
            "mdi:thermometer-chevron-up" if heating else "mdi:thermometer-chevron-down"
        )
        unit = manager.hass.config.units.temperature_unit
        self._attr_native_unit_of_measurement = unit
        if unit == UnitOfTemperature.CELSIUS:
            self._attr_native_min_value = -20
            self._attr_native_max_value = 55
            default = DEFAULT_HEATING_LOCKOUT_C if heating else DEFAULT_COOLING_LOCKOUT_C
        else:
            self._attr_native_min_value = 0
            self._attr_native_max_value = 130
            default = DEFAULT_HEATING_LOCKOUT_F if heating else DEFAULT_COOLING_LOCKOUT_F
        self._attr_native_value = default
        self._push(default)

    def _push(self, value: float) -> None:
        self._manager.lockout_temp = float(value)


class ChangeoverDelayNumber(_ManagerNumber):
    """Minimum time the other direction must be off before this one runs.

    Set in minutes. The interlock (never both at once) holds even at 0.
    """

    _attr_name = "Changeover delay"
    _attr_icon = "mdi:swap-horizontal"
    _attr_native_min_value = 0
    _attr_native_max_value = 240
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_changeover_delay"
        self._attr_native_value = DEFAULT_CHANGEOVER_MINUTES
        self._push(DEFAULT_CHANGEOVER_MINUTES)

    def _push(self, value: float) -> None:
        self._manager.changeover_delay = int(value) * 60


class ChangeoverBandNumber(_ManagerNumber):
    """How far past setpoint an auto thermostat's room must swing before
    the system changes over to this direction. 0 turns it off."""

    _attr_name = "Changeover band"
    _attr_icon = "mdi:arrow-expand-vertical"
    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 0.5

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_changeover_band"
        unit = manager.hass.config.units.temperature_unit
        self._attr_native_unit_of_measurement = unit
        default = (
            DEFAULT_CHANGEOVER_BAND_C
            if unit == UnitOfTemperature.CELSIUS
            else DEFAULT_CHANGEOVER_BAND_F
        )
        self._attr_native_value = default
        self._push(default)

    def _push(self, value: float) -> None:
        self._manager.changeover_band = float(value)


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
