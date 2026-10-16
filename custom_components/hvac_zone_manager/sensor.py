"""Sensors for the HVAC Zone Manager: fleet status, per-zone status,
and runtime tracking."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MODE_HEATING
from .manager import CoolingZoneManager

# The runtime sensors poll on this cadence so their values keep ticking
# while a zone is running between state changes.
SCAN_INTERVAL = timedelta(seconds=30)

ZONE_STATES = [
    "cooling",
    "heating",
    "companion",
    "winding_down",
    "cooldown",
    "waiting",
    "idle",
]

STATE_ICONS = {
    "cooling": "mdi:snowflake",
    "heating": "mdi:fire",
    "companion": "mdi:link-variant",
    "winding_down": "mdi:snowflake-melt",
    "cooldown": "mdi:snowflake-off",
    "waiting": "mdi:timer-sand",
    "idle": "mdi:power-standby",
}
HEATING_ICONS = {
    "winding_down": "mdi:fire-off",
    "cooldown": "mdi:fire-off",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the status and runtime sensors."""
    manager: CoolingZoneManager = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        ZoneStatusSensor(manager, entry),
        TotalRuntimeSensor(manager, entry),
        CooldownSensor(manager, entry),
    ]
    for zone in manager.zones:
        entities.append(ZoneDetailSensor(manager, entry, zone.name))
        entities.append(ZoneRuntimeSensor(manager, entry, zone.name))
        entities.append(ZoneCountdownSensor(manager, entry, zone.name))
    async_add_entities(entities)


class _ManagerSensor(SensorEntity):
    """Base for sensors that refresh whenever the manager reconciles."""

    _attr_has_entity_name = True
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

    async def async_added_to_hass(self) -> None:
        """Update whenever the manager reconciles."""
        self.async_on_remove(self._manager.add_listener(self.async_write_ha_state))


class ZoneStatusSensor(_ManagerSensor):
    """Shows how many zones are cooling, with full detail as attributes."""

    _attr_name = "Active zones"
    _attr_icon = "mdi:hvac"
    _attr_native_unit_of_measurement = "zones"

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

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
            "resting": manager.resting_zones,
            "preempted": manager.preempted_zones,
            "round_robin_order": manager.rr_order,
            "max_zones": manager.max_zones,
            "overlap_seconds": manager.overlap,
            "max_run_seconds": manager.max_run,
            "cooldown_seconds": manager.cooldown,
            "cooldown_until": (
                manager.cooldown_until.isoformat()
                if manager.cooldown_until
                else None
            ),
            "outdoor_temp": manager.outdoor_temp,
            "temp_allowed_zones": manager.temp_allowed_zones,
            "effective_max_zones": manager.effective_max_zones,
            "mode": manager.mode,
            "min_zones": manager.min_zones,
            "companions": manager.companion_zones,
            "short_of_min_zones": manager.short_of_floor,
            "zones": {
                zone.name: {
                    "status": manager.zone_status(zone.name),
                    "priority": manager.zone_priority(zone.name),
                    "requesting": manager.zone_requesting(zone.name),
                    manager.active_word: manager.zone_cooling(zone.name),
                    "cycle_runtime_seconds": round(
                        manager.zone_cycle_runtime(zone.name)
                    ),
                }
                for zone in manager.zones
            },
            "session_runtime_seconds": round(manager.session_runtime),
            "version": manager.version,
        }


class ZoneDetailSensor(_ManagerSensor):
    """One zone's live status: cooling, winding_down, waiting, or idle."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ZONE_STATES

    def __init__(
        self, manager: CoolingZoneManager, entry: ConfigEntry, zone_name: str
    ) -> None:
        super().__init__(manager, entry)
        self._zone_name = zone_name
        self._attr_name = f"{zone_name} status"
        self._attr_unique_id = f"{entry.entry_id}_zone_{zone_name}_status"

    @property
    def native_value(self) -> str:
        return self._manager.zone_status(self._zone_name)

    @property
    def icon(self) -> str:
        state = self.native_value
        if self._manager.mode == MODE_HEATING and state in HEATING_ICONS:
            return HEATING_ICONS[state]
        return STATE_ICONS.get(state, "mdi:hvac")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        manager = self._manager
        name = self._zone_name
        zone = manager.get_zone(name)
        started = manager.zone_started_at(name)
        winddown = manager.zone_winddown_until(name)
        resting = manager.zone_resting_until(name)
        countdown = manager.zone_countdown(name)
        rr = manager.rr_order
        return {
            "request_entity": zone.request_entity,
            "request_on": manager.zone_requesting(name),
            "switch_entity": zone.switch_entity,
            f"{manager.active_word}_on": manager.zone_cooling(name),
            "companion": name in manager.companion_zones,
            "climate_entity": zone.climate_entity,
            "priority": manager.zone_priority(name),
            "preempted": name in manager.preempted_zones,
            "started_at": started.isoformat() if started else None,
            "cycle_runtime_seconds": round(manager.zone_cycle_runtime(name)),
            "last_cycle_seconds": round(manager.zone_last_cycle(name)),
            "winddown_until": winddown.isoformat() if winddown else None,
            "resting_until": resting.isoformat() if resting else None,
            "countdown": countdown[0].isoformat() if countdown else None,
            "countdown_type": countdown[1] if countdown else None,
            "round_robin_position": rr.index(name) + 1 if name in rr else None,
        }


class _RuntimeSensor(_ManagerSensor):
    """Base for the cycle/session runtime sensors."""

    # Event-driven updates plus polling, so the value keeps ticking while
    # a zone runs. These are measurements, not lifetime totals: the zone
    # sensors reset when a run starts, the session sensor when a new
    # cooling session begins.
    _attr_should_poll = True
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:timer-outline"


class ZoneRuntimeSensor(_RuntimeSensor):
    """How long one zone's current run has been cooling.

    Resets to zero when the zone starts a run; while the zone is off it
    holds the duration of the last completed run.
    """

    def __init__(
        self, manager: CoolingZoneManager, entry: ConfigEntry, zone_name: str
    ) -> None:
        super().__init__(manager, entry)
        self._zone_name = zone_name
        self._attr_name = f"{zone_name} cycle runtime"
        self._attr_unique_id = f"{entry.entry_id}_zone_{zone_name}_runtime"

    @property
    def native_value(self) -> int:
        return round(self._manager.zone_cycle_runtime(self._zone_name))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        manager = self._manager
        started = manager.zone_started_at(self._zone_name)
        return {
            "running": started is not None,
            "started_at": started.isoformat() if started else None,
            "last_cycle_seconds": round(manager.zone_last_cycle(self._zone_name)),
        }


class TotalRuntimeSensor(_RuntimeSensor):
    """Cooling time across all zones in the current session.

    Resets to zero when a zone requests again after every zone was
    satisfied and switched off.
    """

    _attr_name = "Session runtime"
    _attr_icon = "mdi:timer"

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_total_runtime"

    @property
    def native_value(self) -> int:
        return round(self._manager.session_runtime)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        manager = self._manager
        since = manager.session_started
        return {
            "session_started": since.isoformat() if since else None,
            "per_zone_cycle_seconds": {
                zone.name: round(manager.zone_cycle_runtime(zone.name))
                for zone in manager.zones
            },
        }


class _CountdownSensor(_ManagerSensor):
    """Base for the timestamp countdown sensors.

    The frontend renders a timestamp sensor as a live countdown ("in 4
    minutes"). Event-driven updates plus a slow poll, so an expired
    countdown clears itself even when no state change follows it.
    """

    _attr_should_poll = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP


class CooldownSensor(_CountdownSensor):
    """When the compressor cooldown ends; unknown while none is running.

    The cooldown starts the moment the last zone switches off and blocks
    every zone start until it expires, so the compressor cannot short
    cycle. Overlap handoffs are unaffected (the compressor never stops).
    """

    _attr_name = "Cooldown ends"
    _attr_icon = "mdi:timer-sand"

    def __init__(self, manager: CoolingZoneManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_cooldown_until"

    @property
    def native_value(self) -> Any:
        return self._manager.cooldown_until

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        manager = self._manager
        off_at = manager.last_compressor_off
        return {
            "active": manager.cooldown_until is not None,
            "cooldown_seconds": manager.cooldown,
            "compressor_off_at": off_at.isoformat() if off_at else None,
        }


class ZoneCountdownSensor(_CountdownSensor):
    """The zone's next scheduled transition, as a live countdown.

    Whatever delay currently applies to the zone — wind-down, rest gate,
    compressor cooldown, priority guard, or a pending max-run rotation —
    this shows when it ends; the ``countdown_type`` attribute says which
    one it is. Unknown when nothing is pending for the zone.
    """

    _attr_icon = "mdi:progress-clock"

    def __init__(
        self, manager: CoolingZoneManager, entry: ConfigEntry, zone_name: str
    ) -> None:
        super().__init__(manager, entry)
        self._zone_name = zone_name
        self._attr_name = f"{zone_name} countdown"
        self._attr_unique_id = f"{entry.entry_id}_zone_{zone_name}_countdown"

    @property
    def native_value(self) -> Any:
        countdown = self._manager.zone_countdown(self._zone_name)
        return countdown[0] if countdown else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        countdown = self._manager.zone_countdown(self._zone_name)
        return {"countdown_type": countdown[1] if countdown else None}
