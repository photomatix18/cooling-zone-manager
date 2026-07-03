"""The reconciler state machine that arbitrates cooling zones.

Same behavior as the YAML v2 automation, implemented natively:

* A zone starts as soon as it requests, if capacity allows.
* Fairness is round-robin: the least recently started zone gets the next
  free slot.
* When a zone loses its request, the next waiting zone starts immediately
  and the old zone keeps running for ``overlap`` seconds before being
  switched off ("overlap handoff").
* If the old zone's request comes back during the overlap, it simply keeps
  running (no short-cycling).
* Lowering ``max_zones`` trims the oldest-started zones immediately.
* Everything is re-derived from live entity states on every pass, so the
  manager self-heals after restarts, reloads, or manual switch changes.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


@dataclass
class Zone:
    """One managed cooling zone."""

    name: str
    request_entity: str
    switch_entity: str


class CoolingZoneManager:
    """Keep at most ``max_zones`` cooling switches on at a time."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        zones: list[Zone],
        max_zones: int,
        overlap: int,
    ) -> None:
        self.hass = hass
        self.zones = zones
        self.max_zones = int(max_zones)
        self.overlap = int(overlap)

        self._by_name = {zone.name: zone for zone in zones}
        # Round-robin order, least recently started first.
        self._rr: list[str] = [zone.name for zone in zones]
        # zone name -> cancel callback for its wind-down timer
        self._winddown: dict[str, CALLBACK_TYPE] = {}
        self._lock = asyncio.Lock()
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_start: CALLBACK_TYPE | None = None
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self._listeners: list[CALLBACK_TYPE] = []

    # ------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        """Load saved round-robin order, subscribe to changes, reconcile."""
        data = await self._store.async_load()
        if data and isinstance(data.get("rr"), list):
            known = set(self._by_name)
            saved = [name for name in data["rr"] if name in known]
            # Self-healing: any zone missing from the saved order is appended.
            self._rr = saved + [name for name in self._rr if name not in saved]

        entities: list[str] = []
        for zone in self.zones:
            entities.append(zone.request_entity)
            entities.append(zone.switch_entity)
        self._unsub_state = async_track_state_change_event(
            self.hass, entities, self._handle_state_event
        )

        @callback
        def _initial_reconcile(_hass: HomeAssistant) -> None:
            self.hass.async_create_task(self.async_reconcile())

        # Wait until HA is fully started so entity states are real, not
        # "unavailable" placeholders from integrations still loading.
        self._unsub_start = async_at_started(self.hass, _initial_reconcile)

    async def async_stop(self) -> None:
        """Tear down listeners and timers."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_start is not None:
            self._unsub_start()
            self._unsub_start = None
        for cancel in self._winddown.values():
            cancel()
        self._winddown.clear()

    # ------------------------------------------------------------ listeners

    @callback
    def add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Register an entity update callback; returns an unsubscribe."""
        self._listeners.append(update)

        @callback
        def _remove() -> None:
            if update in self._listeners:
                self._listeners.remove(update)

        return _remove

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    # -------------------------------------------------------- state helpers

    def _is_on(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == "on"

    @property
    def active_zones(self) -> list[str]:
        return [z.name for z in self.zones if self._is_on(z.switch_entity)]

    @property
    def requesting_zones(self) -> list[str]:
        return [z.name for z in self.zones if self._is_on(z.request_entity)]

    @property
    def winding_down(self) -> list[str]:
        return list(self._winddown)

    @property
    def waiting_zones(self) -> list[str]:
        on = set(self.active_zones)
        return [
            name
            for name in self._rr
            if name not in on and self._is_on(self._by_name[name].request_entity)
        ]

    @property
    def rr_order(self) -> list[str]:
        return list(self._rr)

    # -------------------------------------------------------------- events

    @callback
    def _handle_state_event(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state not in ("on", "off"):
            return  # ignore unavailable/unknown flapping
        self.hass.async_create_task(self.async_reconcile())

    # ----------------------------------------------------------- reconciler

    async def async_reconcile(self) -> None:
        """Converge the switches toward the desired state. Idempotent."""
        async with self._lock:
            await self._reconcile()
        self._notify()

    async def _reconcile(self) -> None:
        on = {z.name for z in self.zones if self._is_on(z.switch_entity)}
        requesting = {z.name for z in self.zones if self._is_on(z.request_entity)}

        # 1) Clean up stale timers, and re-admit zones whose request came
        #    back during their overlap (this is the anti-short-cycle fix).
        for name in list(self._winddown):
            if name not in on:
                self._cancel_winddown(name)
            elif name in requesting:
                self._cancel_winddown(name)
                _LOGGER.info(
                    "Zone '%s' re-requested during overlap; it stays on", name
                )

        # 2) Begin wind-down for zones that lost their request. The zone
        #    keeps cooling until its timer fires.
        for name in on - requesting:
            if name not in self._winddown:
                self._schedule_winddown(name)
                _LOGGER.info(
                    "Zone '%s' lost its request; winding down for %s s",
                    name,
                    self.overlap,
                )

        winding = set(self._winddown)
        countable = on - winding  # winding zones no longer occupy a slot

        # 3) Capacity trim: immediate, oldest-started first.
        excess = len(countable) - self.max_zones
        if excess > 0:
            for name in [n for n in self._rr if n in countable][:excess]:
                _LOGGER.warning(
                    "Capacity trim: switching off zone '%s' (max_zones=%s)",
                    name,
                    self.max_zones,
                )
                await self._turn_off(name)
                countable.discard(name)
                on.discard(name)

        # 4) Start waiting zones into free capacity, round-robin order.
        #    The winding-down zones freeing their slot early is what creates
        #    the overlap: the replacement turns on now, the old zone turns
        #    off when its timer fires.
        for name in list(self._rr):
            effective = len(on) - len(winding)
            if (
                name not in on
                and name in requesting
                and effective < self.max_zones
                # At most one zone of handoff surplus at a time; matches the
                # original automation's behavior.
                and len(on) < self.max_zones + 1
            ):
                self._rr.remove(name)
                self._rr.append(name)
                await self._save_rr()
                _LOGGER.info(
                    "Starting zone '%s' | round-robin order now: %s",
                    name,
                    self._rr,
                )
                await self._turn_on(name)
                on.add(name)

    # ------------------------------------------------------------ wind-down

    def _schedule_winddown(self, name: str) -> None:
        @callback
        def _expired(_now) -> None:
            self.hass.async_create_task(self._winddown_expired(name))

        self._cancel_winddown(name)
        self._winddown[name] = async_call_later(self.hass, self.overlap, _expired)

    def _cancel_winddown(self, name: str) -> None:
        cancel = self._winddown.pop(name, None)
        if cancel is not None:
            cancel()

    async def _winddown_expired(self, name: str) -> None:
        async with self._lock:
            self._winddown.pop(name, None)
            zone = self._by_name[name]
            # Only switch off if the request is still gone. If it came back
            # in a race, the zone keeps running.
            if self._is_on(zone.switch_entity) and not self._is_on(
                zone.request_entity
            ):
                _LOGGER.info("Zone '%s' overlap expired; switching off", name)
                await self._turn_off(name)
        # The resulting state change triggers another reconcile pass, which
        # fills the freed slot if anything is waiting.
        self._notify()

    # ------------------------------------------------------------- actions

    async def _turn_on(self, name: str) -> None:
        await self.hass.services.async_call(
            "homeassistant",
            "turn_on",
            {"entity_id": self._by_name[name].switch_entity},
            blocking=True,
        )

    async def _turn_off(self, name: str) -> None:
        await self.hass.services.async_call(
            "homeassistant",
            "turn_off",
            {"entity_id": self._by_name[name].switch_entity},
            blocking=True,
        )

    async def _save_rr(self) -> None:
        await self._store.async_save({"rr": list(self._rr)})
