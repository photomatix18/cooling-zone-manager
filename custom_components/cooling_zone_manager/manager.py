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
* If ``max_run`` is set and a zone has been cooling that long while other
  zones are waiting, it is rotated out with the same overlap handoff and
  sent to the back of the round-robin queue. A zone with no competition is
  never cut off.
* A zone that just finished a run must rest — off for as long as it last ran,
  capped at ``max_run`` — before it can *force* another zone out. Without
  this, a rotated-out zone whose wind-down expires while it is the only
  waiter instantly evicts the other long-runner (whose clock never reset
  during the handoff) and re-seats itself with a zero-second off blink.
  Resting zones still take naturally-freed capacity immediately.
* Lowering ``max_zones`` trims the oldest-started zones immediately.
* Everything is re-derived from live entity states on every pass, so the
  manager self-heals after restarts, reloads, or manual switch changes.

The manager also tracks each zone's current cycle runtime (reset when the
zone starts a run) and the total for the current cooling session (reset when
demand returns after every zone was satisfied), persisted across restarts.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STORAGE_VERSION, TEMP_HYSTERESIS

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
        max_run: int = 0,
        temp_entity: str | None = None,
    ) -> None:
        self.hass = hass
        self.zones = zones
        self.max_zones = int(max_zones)
        self.overlap = int(overlap)
        # Longest a zone may run while others wait; 0 disables the limit.
        self.max_run = int(max_run)
        # Optional outdoor temperature sensor for temperature-aware capacity.
        self.temp_entity = temp_entity
        # tier -> outdoor temperature below which that many zones may run;
        # pushed by the threshold number entities.
        self.zone_thresholds: dict[int, float] = {}
        # Last outdoor reading and the zone count it currently allows.
        self.outdoor_temp: float | None = None
        self._temp_tier: int | None = None
        # Integration version, filled in by async_setup_entry for display.
        self.version: str | None = None

        self._by_name = {zone.name: zone for zone in zones}
        # Round-robin order, least recently started first.
        self._rr: list[str] = [zone.name for zone in zones]
        # zone name -> cancel callback for its wind-down timer
        self._winddown: dict[str, CALLBACK_TYPE] = {}
        # Zones winding down early: name -> reason ("max_run" or "temp").
        # They are not re-admitted during the overlap even though they
        # still request.
        self._preempted: dict[str, str] = {}
        # zone name -> (fire-at epoch, cancel) for its max-run timer
        self._maxrun_timers: dict[str, tuple[float, CALLBACK_TYPE]] = {}
        # (fire-at epoch, cancel) for the single rest timer: a reconcile
        # scheduled for when a resting waiter may force a rotation again.
        self._rest_timer: tuple[float, CALLBACK_TYPE] | None = None
        # Runtime bookkeeping: when each running zone started (epoch), how
        # long each zone's last completed run took, and the completed-run
        # seconds of the current cooling session.
        self._started: dict[str, float] = {}
        self._last_cycle: dict[str, float] = {}
        # zone name -> when its last completed run ended (epoch); drives
        # the rest requirement before it may force a rotation.
        self._last_run_end: dict[str, float] = {}
        self._session_runtime = 0.0
        self._session_started: float | None = None
        # True once every zone is satisfied and switched off; the next
        # request after that starts a fresh session.
        self._idle = False
        self._restored_started: dict[str, float] = {}
        self._dirty = False
        self._lock = asyncio.Lock()
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_start: CALLBACK_TYPE | None = None
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self._listeners: list[CALLBACK_TYPE] = []

    # ------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        """Load saved state, subscribe to changes, reconcile."""
        data = await self._store.async_load()
        if data:
            known = set(self._by_name)
            if isinstance(data.get("rr"), list):
                saved = [name for name in data["rr"] if name in known]
                # Self-healing: any zone missing from the saved order is
                # appended.
                self._rr = saved + [name for name in self._rr if name not in saved]
            if isinstance(data.get("last_cycle"), dict):
                self._last_cycle = {
                    name: float(value)
                    for name, value in data["last_cycle"].items()
                    if name in known
                }
            if isinstance(data.get("last_run_end"), dict):
                self._last_run_end = {
                    name: float(value)
                    for name, value in data["last_run_end"].items()
                    if name in known
                }
            if isinstance(data.get("started"), dict):
                # Consumed by the first runtime pass for zones still on, so
                # a run in progress across a restart keeps its start time.
                self._restored_started = {
                    name: float(value)
                    for name, value in data["started"].items()
                    if name in known
                }
            if data.get("session_runtime") is not None:
                self._session_runtime = float(data["session_runtime"])
            if data.get("session_started") is not None:
                self._session_started = float(data["session_started"])
            self._idle = bool(data.get("idle", False))

        entities: list[str] = []
        for zone in self.zones:
            entities.append(zone.request_entity)
            entities.append(zone.switch_entity)
        if self.temp_entity:
            entities.append(self.temp_entity)
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
        """Tear down listeners and timers, flush runtime totals."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_start is not None:
            self._unsub_start()
            self._unsub_start = None
        for cancel in self._winddown.values():
            cancel()
        self._winddown.clear()
        for _fire_at, cancel in self._maxrun_timers.values():
            cancel()
        self._maxrun_timers.clear()
        if self._rest_timer is not None:
            self._rest_timer[1]()
            self._rest_timer = None

        # Runs in progress keep their original start marker: on reload the
        # cycle continues from its true start, nothing double-counts.
        await self._save_state()

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

    def get_zone(self, name: str) -> Zone:
        return self._by_name[name]

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
    def preempted_zones(self) -> list[str]:
        return sorted(self._preempted)

    @property
    def temp_allowed_zones(self) -> int | None:
        """Zone count the outdoor temperature currently allows, or None."""
        return self._temp_tier

    @property
    def effective_max_zones(self) -> int:
        """The manual cap, further limited by the outdoor temperature."""
        if self._temp_tier is None:
            return self.max_zones
        return max(1, min(self.max_zones, self._temp_tier))

    @property
    def waiting_zones(self) -> list[str]:
        on = set(self.active_zones)
        return [
            name
            for name in self._rr
            if name not in on and self._is_on(self._by_name[name].request_entity)
        ]

    @property
    def resting_zones(self) -> list[str]:
        """Waiting zones still resting after their last run.

        They may take freed capacity, but cannot force a rotation yet.
        """
        if self.max_run <= 0:
            return []
        now = dt_util.utcnow().timestamp()
        return [name for name in self.waiting_zones if self._rested_at(name) > now]

    @property
    def rr_order(self) -> list[str]:
        return list(self._rr)

    # ------------------------------------------------------- runtime helpers

    def zone_requesting(self, name: str) -> bool:
        return self._is_on(self._by_name[name].request_entity)

    def zone_cooling(self, name: str) -> bool:
        return self._is_on(self._by_name[name].switch_entity)

    def zone_status(self, name: str) -> str:
        """One-word status: cooling, winding_down, waiting, or idle."""
        if name in self._winddown:
            return "winding_down"
        if self.zone_cooling(name):
            return "cooling"
        if self.zone_requesting(name):
            return "waiting"
        return "idle"

    def zone_started_at(self, name: str) -> datetime | None:
        """When the zone's current run began, or None if not running."""
        start = self._started.get(name)
        if start is None:
            return None
        return dt_util.utc_from_timestamp(start)

    def zone_current_run(self, name: str) -> float:
        """Seconds the zone has been on in its current run (0 if off)."""
        start = self._started.get(name)
        if start is None:
            return 0.0
        return max(0.0, dt_util.utcnow().timestamp() - start)

    def zone_cycle_runtime(self, name: str) -> float:
        """Seconds of the zone's current run, or of its last completed run.

        Resets to zero the moment the zone starts a new run.
        """
        start = self._started.get(name)
        if start is not None:
            return max(0.0, dt_util.utcnow().timestamp() - start)
        return self._last_cycle.get(name, 0.0)

    def zone_last_cycle(self, name: str) -> float:
        """Seconds of the zone's last completed run."""
        return self._last_cycle.get(name, 0.0)

    @property
    def session_runtime(self) -> float:
        """Seconds cooled across all zones in the current cooling session.

        A session starts when demand returns to a fully quiet system and
        the total resets to zero at that moment.
        """
        now = dt_util.utcnow().timestamp()
        return self._session_runtime + sum(
            max(0.0, now - start) for start in self._started.values()
        )

    @property
    def session_started(self) -> datetime | None:
        """When the current cooling session began."""
        if self._session_started is None:
            return None
        return dt_util.utc_from_timestamp(self._session_started)

    # ------------------------------------------------- temperature capacity

    def _outdoor_reading(self) -> float | None:
        if not self.temp_entity:
            return None
        state = self.hass.states.get(self.temp_entity)
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _tier_for(self, temp: float, offset: float) -> int:
        """Zones allowed at ``temp``, with thresholds shifted by ``offset``."""
        tier = 1
        for k in range(2, len(self.zones) + 1):
            threshold = self.zone_thresholds.get(k)
            if threshold is None or temp >= threshold + offset:
                break
            tier = k
        return tier

    def _update_temp_tier(self) -> None:
        """Re-derive the temperature-allowed zone count, with hysteresis.

        Raising capacity requires the temperature to be a full hysteresis
        band below the threshold; lowering requires it a band above. A
        reading hovering exactly at a threshold changes nothing.
        """
        temp = self._outdoor_reading()
        self.outdoor_temp = temp
        if temp is None or not self.zone_thresholds:
            # No sensor, sensor unavailable, or no thresholds set: the
            # manual max applies unchanged.
            self._temp_tier = None
            return
        current = self._temp_tier
        if current is None:
            self._temp_tier = self._tier_for(temp, 0.0)
            return
        raise_to = self._tier_for(temp, -TEMP_HYSTERESIS)
        lower_to = self._tier_for(temp, TEMP_HYSTERESIS)
        if raise_to > current:
            self._temp_tier = raise_to
        elif lower_to < current:
            self._temp_tier = lower_to
        if self._temp_tier != current:
            _LOGGER.info(
                "Outdoor temperature %.1f now allows %s zone(s)",
                temp,
                self._temp_tier,
            )

    # -------------------------------------------------------------- events

    @callback
    def _handle_state_event(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        if event.data.get("entity_id") == self.temp_entity:
            # Any temperature change may move the capacity tier.
            self.hass.async_create_task(self.async_reconcile())
            return
        if new_state.state not in ("on", "off"):
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

        # 0) Runtime bookkeeping, derived from the same observed states.
        self._track_runtime(on, requesting)

        # Effective capacity for this pass: the manual cap, possibly
        # lowered by the outdoor temperature.
        self._update_temp_tier()
        limit = self.effective_max_zones

        # 1) Clean up stale timers, and re-admit zones whose request came
        #    back during their overlap (this is the anti-short-cycle fix).
        #    Preempted zones are the exception: they still request, but they
        #    used up their turn, so they finish their wind-down anyway.
        if self.max_run <= 0:
            # The max-run limit was just disabled; its pending preemptions
            # are void and those zones get re-admitted below like any
            # re-requester. Temperature sheds are unaffected.
            for name in [
                n for n, reason in self._preempted.items() if reason == "max_run"
            ]:
                del self._preempted[name]
        for name in list(self._winddown):
            if name not in on:
                self._cancel_winddown(name)
                self._preempted.pop(name, None)
            elif name in requesting and name not in self._preempted:
                self._cancel_winddown(name)
                _LOGGER.info(
                    "Zone '%s' re-requested during overlap; it stays on", name
                )
        self._preempted = {
            name: reason
            for name, reason in self._preempted.items()
            if name in self._winddown
        }

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

        # 3) Max-run rotation: if a zone has been cooling past the limit
        #    while others wait, rotate it out (overlap handoff, back of the
        #    round-robin queue). One zone per pass keeps handoffs smooth.
        #    Only rested waiters count: a zone that just gave up its slot
        #    may not force another zone out (its own wind-down expiry would
        #    otherwise re-seat it instantly, evicting whichever zone ran
        #    through the handoff). It can still take freed capacity below.
        rest_check_at: float | None = None
        if self.max_run > 0:
            winding = set(self._winddown)
            now = dt_util.utcnow().timestamp()
            over = [
                name
                for name in self._rr
                if name in on
                and name not in winding
                and self._started.get(name) is not None
                and now - self._started[name] >= self.max_run
            ]
            if over:
                waiting: list[str] = []
                resting: list[float] = []
                for name in self._rr:
                    if name in on or name not in requesting:
                        continue
                    rested_at = self._rested_at(name)
                    if rested_at <= now:
                        waiting.append(name)
                    else:
                        resting.append(rested_at)
                if waiting:
                    name = over[0]
                    _LOGGER.info(
                        "Zone '%s' hit max run time (%s s) with zones waiting;"
                        " rotating it out",
                        name,
                        self.max_run,
                    )
                    self._preempted[name] = "max_run"
                    self._schedule_winddown(name)
                    self._rr.remove(name)
                    self._rr.append(name)
                    self._dirty = True
                elif resting:
                    # Every waiter is still resting: re-check when the
                    # first of them has rested long enough.
                    rest_check_at = min(resting)
        self._update_rest_timer(rest_check_at)

        # 4) Temperature shed: when the outdoor temperature lowers the
        #    capacity below what is running, wind the excess down gracefully
        #    (oldest-started first) instead of cutting power instantly.
        if limit < self.max_zones:
            winding = set(self._winddown)
            countable = on - winding
            excess = len(countable) - limit
            if excess > 0:
                for name in [n for n in self._rr if n in countable][:excess]:
                    _LOGGER.info(
                        "Outdoor temperature limits capacity to %s zone(s);"
                        " winding down zone '%s'",
                        limit,
                        name,
                    )
                    self._preempted[name] = "temp"
                    self._schedule_winddown(name)
                    self._rr.remove(name)
                    self._rr.append(name)
                    self._dirty = True

        winding = set(self._winddown)
        countable = on - winding  # winding zones no longer occupy a slot

        # 5) Manual capacity trim: immediate, oldest-started first.
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

        # 6) Start waiting zones into free capacity, round-robin order.
        #    The winding-down zones freeing their slot early is what creates
        #    the overlap: the replacement turns on now, the old zone turns
        #    off when its timer fires.
        for name in list(self._rr):
            effective = len(on) - len(winding)
            if (
                name not in on
                and name in requesting
                and effective < limit
                # At most one zone of handoff surplus at a time; matches the
                # original automation's behavior.
                and len(on) < limit + 1
            ):
                self._rr.remove(name)
                self._rr.append(name)
                self._dirty = True
                _LOGGER.info(
                    "Starting zone '%s' | round-robin order now: %s",
                    name,
                    self._rr,
                )
                await self._turn_on(name)
                on.add(name)
                # Record the start now rather than waiting for the switch's
                # state-change event, so runtime tracking and the max-run
                # timer are already correct in this same pass.
                self._started.setdefault(name, dt_util.utcnow().timestamp())

        # 7) Keep a timer on every running zone so a reconcile pass fires
        #    the moment it crosses the max-run threshold.
        self._update_maxrun_timers(on)

        if self._dirty:
            self._dirty = False
            await self._save_state()

    # ------------------------------------------------------ runtime tracking

    def _track_runtime(self, on: set[str], requesting: set[str]) -> None:
        """Derive run starts/ends, cycle durations, and the session total."""
        now = dt_util.utcnow().timestamp()

        # A new session begins when demand returns to a fully quiet system
        # (nothing requesting, nothing running): the total resets to zero.
        active = bool(requesting or on)
        if active and (self._idle or self._session_started is None):
            self._session_runtime = 0.0
            self._session_started = now
            self._dirty = True
            _LOGGER.info("New cooling session started; session total reset")
        if self._idle != (not active):
            self._idle = not active
            self._dirty = True

        for zone in self.zones:
            name = zone.name
            if name in on:
                if name not in self._started:
                    restored = self._restored_started.pop(name, None)
                    state = self.hass.states.get(zone.switch_entity)
                    observed = (
                        state.last_changed.timestamp() if state is not None else now
                    )
                    # A marker saved at shutdown/unload already has the run
                    # up to that moment banked, so it always wins over the
                    # observed last_changed (which is stale after a reload
                    # and reset after a restart).
                    self._started[name] = restored if restored is not None else observed
                    self._dirty = True
            elif name in self._started:
                start = self._started.pop(name)
                state = self.hass.states.get(zone.switch_entity)
                end = (
                    state.last_changed.timestamp()
                    if state is not None and state.state == "off"
                    else now
                )
                duration = max(0.0, end - start)
                self._last_cycle[name] = duration
                self._last_run_end[name] = end
                self._session_runtime += duration
                self._dirty = True
        # A marker for a zone that is definitively off belongs to a run that
        # ended while HA was down; its tail cannot be recovered, so drop it.
        # Zones still "unavailable" during startup keep their marker until
        # their real state arrives.
        for name in list(self._restored_started):
            state = self.hass.states.get(self._by_name[name].switch_entity)
            if state is not None and state.state == "off":
                del self._restored_started[name]

    # ------------------------------------------------------------ rest gate

    def _rested_at(self, name: str) -> float:
        """When the zone has rested enough to force a rotation again.

        A zone must stay off for as long as its last run lasted (capped at
        ``max_run``) before it may preempt another zone. Zones that never
        ran are always rested.
        """
        end = self._last_run_end.get(name)
        if end is None:
            return 0.0
        return end + min(self.max_run, self._last_cycle.get(name, 0.0))

    def _update_rest_timer(self, fire_at: float | None) -> None:
        """Ensure a reconcile fires when a resting waiter becomes rested."""
        current = self._rest_timer
        if current is not None and (fire_at is None or abs(current[0] - fire_at) > 1):
            current[1]()
            self._rest_timer = None
            current = None
        if fire_at is None or current is not None:
            return

        @callback
        def _expired(_now) -> None:
            self._rest_timer = None
            self.hass.async_create_task(self.async_reconcile())

        now = dt_util.utcnow().timestamp()
        _LOGGER.info(
            "Max-run rotation deferred: all waiting zones are resting;"
            " re-checking in %.0f s",
            fire_at - now,
        )
        self._rest_timer = (
            fire_at,
            async_call_later(self.hass, fire_at - now + 1, _expired),
        )

    # ---------------------------------------------------------- max-run timer

    def _update_maxrun_timers(self, on: set[str]) -> None:
        """Ensure each running zone reconciles when it reaches max_run."""
        now = dt_util.utcnow().timestamp()
        winding = set(self._winddown)
        for zone in self.zones:
            name = zone.name
            fire_at: float | None = None
            start = self._started.get(name)
            if (
                self.max_run > 0
                and name in on
                and name not in winding
                and start is not None
            ):
                candidate = start + self.max_run + 1
                if candidate > now:
                    fire_at = candidate
            current = self._maxrun_timers.get(name)
            if current is not None and (
                fire_at is None or abs(current[0] - fire_at) > 1
            ):
                current[1]()
                del self._maxrun_timers[name]
                current = None
            if fire_at is not None and current is None:

                @callback
                def _expired(_now) -> None:
                    self.hass.async_create_task(self.async_reconcile())

                self._maxrun_timers[name] = (
                    fire_at,
                    async_call_later(self.hass, fire_at - now, _expired),
                )

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
            preempted = name in self._preempted
            self._preempted.pop(name, None)
            zone = self._by_name[name]
            # Only switch off if the request is still gone. If it came back
            # in a race, the zone keeps running. Preempted zones switch off
            # regardless: their request never left, but their turn is over.
            if self._is_on(zone.switch_entity) and (
                preempted or not self._is_on(zone.request_entity)
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

    async def _save_state(self) -> None:
        await self._store.async_save(
            {
                "rr": list(self._rr),
                "started": dict(self._started),
                "last_cycle": dict(self._last_cycle),
                "last_run_end": dict(self._last_run_end),
                "session_runtime": self._session_runtime,
                "session_started": self._session_started,
                "idle": self._idle,
            }
        )
