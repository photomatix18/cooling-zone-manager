"""The reconciler state machine that arbitrates HVAC zones.

One manager handles one direction (an entry's ``mode``): cooling or
heating. Everything below applies to both; "cooling" in the comments
means "running" in whichever direction the entry manages.

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
* A global compressor cooldown: once every zone is off, nothing may start
  again until ``cooldown`` seconds have passed since the last zone switched
  off. Handoffs are unaffected — while any zone is still on (winding down
  included) the compressor never stopped. 0 disables it.
* Lowering ``max_zones`` trims the oldest-started zones immediately.
* Every zone carries a priority (1 runs first, 3 yields to everyone,
  default 2, pushed by the per-zone number entities). Free capacity goes
  to the highest-priority waiter; a runner is only max-run rotated for a
  waiter of its own priority or better; a rested waiter with strictly
  higher priority evicts the worst-priority runner immediately (after a
  short minimum-run guard); sheds and trims take the worst-priority
  zones first. With every zone at the default, all of this collapses to
  plain round-robin.
* A floor, ``min_zones`` (heating entries; 0 or 1 disables it): whenever
  a requesting zone runs and fewer than ``min_zones`` zones are running,
  the manager pulls in *companions* — zones run for the equipment's sake,
  not because their room asked. The companion is the eligible zone
  furthest below its own setpoint (read from its climate entity, which
  must be in this entry's mode). A companion gives its slot back when its
  room passes setpoint + ``companion_margin``, when enough real requests
  arrive, or when the last requester is satisfied. If no zone is
  eligible, the requester runs alone — the floor never keeps a room from
  heating.
* An outdoor lockout (``lockout_temp``, needs the outdoor sensor): a
  cooling entry never runs below it, a heating entry never above it, with
  ``TEMP_HYSTERESIS`` on the way back. Crossing it winds every running
  zone down through the normal overlap; nothing starts until it clears.
* Changeover between a cooling and a heating entry on the same equipment
  (they find each other through ``hass.data``):
  - Interlock: neither starts while the other has any zone on.
  - Delay: after the other direction ran last, this one waits until it
    has been off ``changeover_delay`` seconds.
  - Band: then, for a zone whose thermostat is in ``auto``/``heat_cool``,
    only once its room is ``changeover_band`` past setpoint in this
    direction. Within a direction the thermostat cycles on its own small
    tolerance; switching direction takes a real swing. A thermostat in an
    explicit heat/cool mode skips the band — someone chose the direction.
  Once a zone starts, this direction is the latest to run and the gate is
  gone until the other side runs again. A zone without its own climate
  entity borrows the one of the same-named zone in the other entry.
* Everything is re-derived from live entity states on every pass, so the
  manager self-heals after restarts, reloads, or manual switch changes.
  The one exception is the manager's own recent commands: a template
  switch can take ~20 s to report back, so an unconfirmed turn-on/off is
  trusted over the observed state until it confirms or ``PENDING_GRACE``
  expires. Without that, a reconcile pass in the lag window sees the
  slot still empty, admits a second zone, and the over-capacity shed
  then switches the first zone off ~50 s after it started.

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

from .const import (
    CHANGEOVER_BAND_RECHECK,
    COMPANION_HYSTERESIS,
    DEFAULT_PRIORITY,
    DOMAIN,
    MODE_COOLING,
    MODE_HEATING,
    PENDING_GRACE,
    PRIORITY_MIN_RUN,
    STORAGE_VERSION,
    TEMP_HYSTERESIS,
    TEMP_STARTUP_GRACE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class Zone:
    """One managed zone."""

    name: str
    request_entity: str
    switch_entity: str
    # The zone's thermostat; needed only to choose and release companions.
    climate_entity: str | None = None


class CoolingZoneManager:
    """Keep between ``min_zones`` and ``max_zones`` zone switches on."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        zones: list[Zone],
        max_zones: int,
        overlap: int,
        max_run: int = 0,
        temp_entity: str | None = None,
        cooldown: int = 0,
        mode: str = MODE_COOLING,
        min_zones: int = 0,
        companion_margin: float = 2.0,
    ) -> None:
        self.hass = hass
        self.zones = zones
        self.mode = mode
        # Floor on concurrent zones while any requester runs; <= 1 is off.
        self.min_zones = int(min_zones)
        # Degrees past its own setpoint a companion may go before it gives
        # its slot back.
        self.companion_margin = float(companion_margin)
        # Zones running for load rather than their own request.
        self._companions: set[str] = set()
        # Whether the last pass wanted a companion and found none; only
        # used to log the transition once instead of on every pass.
        self._short_of_floor = False
        self.max_zones = int(max_zones)
        self.overlap = int(overlap)
        # Longest a zone may run while others wait; 0 disables the limit.
        self.max_run = int(max_run)
        # Minimum compressor off time (seconds) after the last zone stops
        # before anything may start again; 0 disables it.
        self.cooldown = int(cooldown)
        # Optional outdoor temperature sensor for temperature-aware capacity.
        self.temp_entity = temp_entity
        # tier -> outdoor temperature below which that many zones may run;
        # pushed by the threshold number entities.
        self.zone_thresholds: dict[int, float] = {}
        # zone name -> priority (1 highest .. 3 lowest); pushed by the
        # per-zone priority number entities. Missing zones default to 2.
        self.zone_priorities: dict[str, int] = {}
        # Last outdoor reading and the zone count it currently allows.
        self.outdoor_temp: float | None = None
        self._temp_tier: int | None = None
        # Outdoor lockout line, pushed by its number entity; None is off.
        self.lockout_temp: float | None = None
        self._locked_out = False
        # Changeover to this direction after the other one ran: minimum
        # off time of the other side (seconds), and the room swing past
        # setpoint an auto thermostat needs. Pushed by number entities.
        self.changeover_delay = 0
        self.changeover_band = 0.0
        # Whether any zone was on at the last pass; going quiet wakes the
        # opposite entry, which may be held by the interlock.
        self._was_running = False
        # When the manager started; bounds the startup capacity freeze.
        self._boot = 0.0
        # Integration version, filled in by async_setup_entry for display.
        self.version: str | None = None

        self._by_name = {zone.name: zone for zone in zones}
        self._climate_entities = {z.climate_entity for z in zones if z.climate_entity}
        # Round-robin order, least recently started first.
        self._rr: list[str] = [zone.name for zone in zones]
        # zone name -> (fire-at epoch, cancel) for its wind-down timer
        self._winddown: dict[str, tuple[float, CALLBACK_TYPE]] = {}
        # Zones winding down early: name -> reason ("max_run", "temp", or
        # "priority"). They are not re-admitted during the overlap even
        # though they still request.
        self._preempted: dict[str, str] = {}
        # zone name -> ("on"/"off", commanded-at epoch): the manager's own
        # switch commands that the switch has not confirmed yet. See the
        # module docstring for why intent must win over observation here.
        self._pending: dict[str, tuple[str, float]] = {}
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
        self._boot = dt_util.utcnow().timestamp()
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
        # Room temperatures and modes decide companions, so their changes
        # need a pass too (min_zones can be raised live, so always track).
        entities.extend(sorted(self._climate_entities))
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
        for _fire_at, cancel in self._winddown.values():
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

    def _zone_on(self, name: str) -> bool:
        """The switch state as the manager must treat it.

        A pending command of the manager's own wins over the observed
        state: the template switches confirm ~20 s late, and believing
        "off" for a zone just commanded on admits a second zone into the
        same slot.
        """
        pending = self._pending.get(name)
        if pending is not None:
            return pending[0] == "on"
        return self._is_on(self._by_name[name].switch_entity)

    def _prune_pending(self) -> None:
        """Drop pending commands the switch confirmed, or that expired."""
        now = dt_util.utcnow().timestamp()
        for name, (intent, since) in list(self._pending.items()):
            observed = self._is_on(self._by_name[name].switch_entity)
            if observed == (intent == "on"):
                del self._pending[name]
            elif now - since > PENDING_GRACE:
                _LOGGER.warning(
                    "Zone '%s' switch never reported '%s' (%.0f s);"
                    " trusting the observed state again",
                    name,
                    intent,
                    now - since,
                )
                del self._pending[name]

    def get_zone(self, name: str) -> Zone:
        return self._by_name[name]

    @property
    def active_zones(self) -> list[str]:
        return [z.name for z in self.zones if self._zone_on(z.name)]

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
        return self._zone_on(name)

    def zone_priority(self, name: str) -> int:
        """The zone's priority: 1 runs first, 3 yields to everyone."""
        return int(self.zone_priorities.get(name, DEFAULT_PRIORITY))

    @property
    def active_word(self) -> str:
        """The running status word for this entry: cooling or heating."""
        return "heating" if self.mode == MODE_HEATING else "cooling"

    @property
    def companion_zones(self) -> list[str]:
        """Zones running for load, not because their room asked."""
        return [z.name for z in self.zones if z.name in self._companions]

    @property
    def short_of_floor(self) -> bool:
        """True while a requester runs below min_zones with no companion."""
        return self._short_of_floor

    def zone_status(self, name: str) -> str:
        """One-word status: cooling or heating, companion, winding_down,
        lockout, changeover, cooldown, waiting, idle."""
        if name in self._winddown:
            return "winding_down"
        if self.zone_cooling(name):
            if name in self._companions:
                return "companion"
            return self.active_word
        if self.zone_requesting(name):
            if self._locked_out:
                return "lockout"
            if self._changeover_holds(name):
                return "changeover"
            if self._cooldown_until_ts() is not None:
                return "cooldown"
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

    # --------------------------------------------------- countdown visibility

    def _cooldown_until_ts(self) -> float | None:
        """Epoch when the compressor cooldown ends, or None if inactive."""
        if self.cooldown <= 0 or not self._last_run_end:
            return None
        if any(self._zone_on(z.name) for z in self.zones):
            return None  # compressor still running, no cooldown in force
        until = max(self._last_run_end.values()) + self.cooldown
        if until <= dt_util.utcnow().timestamp():
            return None
        return until

    @property
    def cooldown_until(self) -> datetime | None:
        """When the compressor cooldown ends, or None if none is running."""
        until = self._cooldown_until_ts()
        if until is None:
            return None
        return dt_util.utc_from_timestamp(until)

    @property
    def last_compressor_off(self) -> datetime | None:
        """When the last zone switched off, while everything is off."""
        if not self._last_run_end:
            return None
        if any(self._zone_on(z.name) for z in self.zones):
            return None
        return dt_util.utc_from_timestamp(max(self._last_run_end.values()))

    def zone_winddown_until(self, name: str) -> datetime | None:
        """When the zone's wind-down expires and it switches off."""
        entry = self._winddown.get(name)
        if entry is None:
            return None
        return dt_util.utc_from_timestamp(entry[0])

    def zone_resting_until(self, name: str) -> datetime | None:
        """When the zone's rest gate opens, while it waits rested-out."""
        if self.max_run <= 0:
            return None
        if self._zone_on(name) or not self.zone_requesting(name):
            return None
        rested_at = self._rested_at(name)
        if rested_at <= dt_util.utcnow().timestamp():
            return None
        return dt_util.utc_from_timestamp(rested_at)

    def zone_countdown(self, name: str) -> tuple[datetime, str] | None:
        """The zone's next scheduled transition, for UI countdowns.

        Returns (when, why) or None when nothing is pending. The reasons:

        * ``switching_off`` — winding down; when the switch turns off.
        * ``priority_eviction`` — running; a rested higher-priority waiter
          takes this slot once the compressor guard clears.
        * ``max_run_rotation`` — running; the max-run limit rotates this
          zone out then (an eligible waiter exists).
        * ``changeover`` — waiting; the changeover delay after the other
          direction ends then.
        * ``compressor_cooldown`` — waiting; the global cooldown ends then.
        * ``resting`` — waiting; its own rest gate opens then.
        * ``waits_for_guard`` — waiting; it evicts a lower-priority runner
          the moment that runner clears the compressor guard.
        * ``waits_for_rotation`` — waiting; a runner reaches its max-run
          limit then and this zone is in line for the slot.

        These are the manager's own timers, published so a zone that looks
        stuck always shows when and why it will move.
        """
        now = dt_util.utcnow().timestamp()
        entry = self._winddown.get(name)
        if entry is not None:
            return dt_util.utc_from_timestamp(entry[0]), "switching_off"

        on = {z.name for z in self.zones if self._zone_on(z.name)}
        requesting = {
            z.name for z in self.zones if self._is_on(z.request_entity)
        }
        limit = self.effective_max_zones
        prio = self.zone_priority

        if name in on:
            start = self._started.get(name)
            if start is None or len(on) < limit:
                return None
            rested, resting = self._waiters(on, requesting, now)
            # A rested higher-priority waiter is poised to take this slot
            # the moment the compressor guard clears — but only the
            # worst-priority runner is the victim.
            guard_end = start + PRIORITY_MIN_RUN
            runners = [r for r in self._rr if r in on and r not in self._winddown]
            if rested and guard_end > now and runners:
                best = min(prio(w) for w in rested)
                worst = max(prio(r) for r in runners)
                if prio(name) > best and prio(name) == worst:
                    return dt_util.utc_from_timestamp(guard_end), "priority_eviction"
            if self.max_run > 0:
                waiters = rested + [w for w, _at in resting]
                if any(prio(w) <= prio(name) for w in waiters):
                    due = start + self.max_run
                    if due > now:
                        return dt_util.utc_from_timestamp(due), "max_run_rotation"
                    # Overdue: the rotation waits on the first eligible
                    # waiter to finish resting.
                    opens = [
                        at
                        for w, at in resting
                        if prio(w) <= prio(name) and at > now
                    ]
                    if opens and not any(prio(w) <= prio(name) for w in rested):
                        return (
                            dt_util.utc_from_timestamp(min(opens)),
                            "max_run_rotation",
                        )
            return None

        if name not in requesting:
            return None

        if self._locked_out:
            return None  # lifts on an outdoor reading, not at a known time
        if not on:
            _state, changeover_until = self._changeover(now)
            if changeover_until is not None:
                return dt_util.utc_from_timestamp(changeover_until), "changeover"
        cooldown = self._cooldown_until_ts()
        if cooldown is not None:
            return dt_util.utc_from_timestamp(cooldown), "compressor_cooldown"
        rested_at = self._rested_at(name) if self.max_run > 0 else 0.0
        if rested_at > now:
            return dt_util.utc_from_timestamp(rested_at), "resting"
        runners = [r for r in self._rr if r in on and r not in self._winddown]
        if not runners or len(on) < limit:
            return None
        # This waiter outranks a runner: it starts when the first outranked
        # runner clears the compressor guard (if one is already clear, the
        # eviction is imminent and there is nothing to count down).
        outranked_ends = [
            self._started[r] + PRIORITY_MIN_RUN
            for r in runners
            if prio(r) > prio(name) and self._started.get(r) is not None
        ]
        if outranked_ends and all(end > now for end in outranked_ends):
            return dt_util.utc_from_timestamp(min(outranked_ends)), "waits_for_guard"
        # Otherwise it waits for a max-run rotation of an eligible runner.
        if self.max_run > 0 and not outranked_ends:
            dues = [
                self._started[r] + self.max_run
                for r in runners
                if prio(r) >= prio(name) and self._started.get(r) is not None
            ]
            future = min((due for due in dues if due > now), default=None)
            if future is not None:
                return dt_util.utc_from_timestamp(future), "waits_for_rotation"
        return None

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
        if not self.temp_entity or not self.zone_thresholds:
            self._temp_tier = None
            return
        if temp is None:
            # Sensor unreadable: keep the last known tier, so a dropout
            # cannot jump capacity to the manual max and back (which
            # admits extra zones and then sheds them). Before the first
            # reading the tier stays unknown: the reconciler freezes
            # capacity changes for TEMP_STARTUP_GRACE, after which the
            # manual max applies — the fail-safe for a dead sensor.
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

    @property
    def locked_out(self) -> bool:
        """True while the outdoor temperature locks this direction out."""
        return self._locked_out

    def _update_lockout(self) -> None:
        """Re-derive the outdoor lockout, with hysteresis on the way out.

        Uses the reading ``_update_temp_tier`` just took. An unreadable
        sensor keeps the last state; before the first reading nothing is
        locked out (fail open — the thermostats keep working as before).
        """
        if self.lockout_temp is None or not self.temp_entity:
            self._locked_out = False
            return
        temp = self.outdoor_temp
        if temp is None:
            return
        line = self.lockout_temp
        if self.mode == MODE_HEATING:
            past = temp > line
            clear = temp <= line - TEMP_HYSTERESIS
        else:
            past = temp < line
            clear = temp >= line + TEMP_HYSTERESIS
        if not self._locked_out and past:
            self._locked_out = True
            _LOGGER.info(
                "Outdoor temperature %.1f is %s the %s lockout (%.1f);"
                " no zone may run",
                temp,
                "above" if self.mode == MODE_HEATING else "below",
                self.active_word,
                line,
            )
        elif self._locked_out and clear:
            self._locked_out = False
            _LOGGER.info(
                "Outdoor temperature %.1f clears the %s lockout",
                temp,
                self.active_word,
            )

    # ----------------------------------------------------------- changeover

    def _opposites(self) -> list[CoolingZoneManager]:
        """Entries of the other direction, on the same equipment."""
        managers = getattr(self.hass, "data", {}).get(DOMAIN, {})
        return [
            m
            for m in managers.values()
            if m is not self and getattr(m, "mode", self.mode) != self.mode
        ]

    def _any_on(self) -> bool:
        return any(self._zone_on(z.name) for z in self.zones)

    def _last_active(self, now: float) -> float | None:
        """When this entry last had a zone running (now, if one still
        is), or None if it never ran.

        A run whose switch just went off but whose end this entry has not
        booked yet (its own pass has not run) still counts as now —
        otherwise the other entry, reconciling first, sees no history and
        changes over with no delay at all.
        """
        if self._started:
            return now
        return max(self._last_run_end.values(), default=None)

    def _changeover(self, now: float) -> tuple[str | None, float | None]:
        """Whether starting in this direction is a changeover, and its gate.

        Returns (state, delay_until). state is None (no changeover: this
        direction ran last, or nothing has run), ``interlock`` (the other
        direction has a zone on), ``delay`` (it stopped less than
        ``changeover_delay`` ago; delay_until says when that ends), or
        ``band`` (the delay is over; each zone must pass the band).
        Only meaningful while none of this entry's zones is on.
        """
        peers = self._opposites()
        if not peers:
            return None, None
        if any(peer._any_on() for peer in peers):
            return "interlock", None
        peer_end = max(
            (end for peer in peers if (end := peer._last_active(now)) is not None),
            default=None,
        )
        if peer_end is None:
            return None, None
        own_end = self._last_active(now)
        if own_end is not None and own_end >= peer_end:
            return None, None
        until = peer_end + self.changeover_delay
        if until > now:
            return "delay", until
        return "band", None

    def _climate_for(self, name: str) -> str | None:
        """The zone's thermostat, borrowed from the other entry if needed.

        Cooling zones are usually set up without one (only companions
        need it); the heating zone of the same name has it.
        """
        entity = self._by_name[name].climate_entity
        if entity:
            return entity
        for peer in self._opposites():
            zone = peer._by_name.get(name)
            if zone is not None and zone.climate_entity:
                return zone.climate_entity
        return None

    def _band_ok(self, name: str) -> bool:
        """Has the zone's room swung far enough to change direction?

        Applies only to a thermostat in ``auto``/``heat_cool``; one set
        to an explicit mode has had its direction chosen for it. Unknown
        thermostat or temperatures pass (the delay still applied).
        """
        if self.changeover_band <= 0:
            return True
        entity = self._climate_for(name)
        state = self.hass.states.get(entity) if entity else None
        if state is None or state.state not in ("auto", "heat_cool"):
            return True
        attrs = state.attributes
        if state.state == "heat_cool":
            key = "target_temp_low" if self.mode == MODE_HEATING else "target_temp_high"
            target = attrs.get(key)
        else:
            target = attrs.get("temperature")
        try:
            target = float(target)
            current = float(attrs.get("current_temperature"))
        except (TypeError, ValueError):
            return True
        if self.mode == MODE_HEATING:
            return current <= target - self.changeover_band
        return current >= target + self.changeover_band

    def _changeover_holds(self, name: str) -> bool:
        """True while the changeover gate keeps this waiting zone off."""
        if self._any_on():
            return False
        state, _until = self._changeover(dt_util.utcnow().timestamp())
        if state in ("interlock", "delay"):
            return True
        return state == "band" and not self._band_ok(name)

    @property
    def changeover_until(self) -> datetime | None:
        """When the changeover delay ends, while it is holding this entry."""
        if self._any_on():
            return None
        _state, until = self._changeover(dt_util.utcnow().timestamp())
        return dt_util.utc_from_timestamp(until) if until else None

    @property
    def changeover_state(self) -> str | None:
        """The changeover gate this entry faces now: interlock, delay,
        band, or None."""
        if self._any_on():
            return None
        return self._changeover(dt_util.utcnow().timestamp())[0]

    @callback
    def _wake_opposites(self) -> None:
        """This direction went quiet; let a held opposite entry re-check."""
        for peer in self._opposites():
            self.hass.async_create_task(peer.async_reconcile())

    # -------------------------------------------------------------- events

    @callback
    def _handle_state_event(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        entity_id = event.data.get("entity_id")
        if entity_id == self.temp_entity or entity_id in self._climate_entities:
            # Any temperature change may move the capacity tier, or start
            # or release a companion.
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
        self._prune_pending()
        on = {z.name for z in self.zones if self._zone_on(z.name)}
        requesting = {z.name for z in self.zones if self._is_on(z.request_entity)}

        # 0) Runtime bookkeeping, derived from the same observed states.
        self._track_runtime(on, requesting)

        # Effective capacity for this pass: the manual cap, possibly
        # lowered by the outdoor temperature.
        self._update_temp_tier()
        self._update_lockout()
        limit = self.effective_max_zones
        capacity_frozen = (
            bool(self.temp_entity)
            and bool(self.zone_thresholds)
            and self._temp_tier is None
            and dt_util.utcnow().timestamp() - self._boot < TEMP_STARTUP_GRACE
        )
        if capacity_frozen:
            # Startup, before the sensor's first reading: hold what is
            # running, start nothing extra, shed nothing. The sensor's
            # first state change (or the grace expiring) unfreezes.
            limit = max(1, min(limit, len(on - set(self._winddown))))

        # 0b) Companions: drop the ones no longer needed or past their
        #     margin. A released companion is simply no longer wanted, so
        #     step 2 winds it down through the normal overlap. Companions
        #     count as wanted from here on, exactly like a request.
        self._retain_companions(on, requesting, limit)
        wanted = requesting | self._companions

        # 1) Clean up stale timers, and re-admit zones whose request came
        #    back during their overlap (this is the anti-short-cycle fix).
        #    Preempted zones are the exception: they still request, but they
        #    used up their turn, so they finish their wind-down anyway.
        #    A zone picked again as a companion is re-admitted the same way.
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
            elif name in wanted and name not in self._preempted:
                self._cancel_winddown(name)
                _LOGGER.info(
                    "Zone '%s' re-requested during overlap; it stays on", name
                )
        self._preempted = {
            name: reason
            for name, reason in self._preempted.items()
            if name in self._winddown
        }

        # 2) Begin wind-down for zones that lost their request (or their
        #    companion role). The zone keeps cooling until its timer fires.
        for name in on - wanted:
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
        #    And only a waiter of the runner's priority or better may force
        #    it out, so a boosted zone is never rotated for a lesser one.
        #    Rotation only happens when the waiter has no free slot to take:
        #    with capacity to spare it simply starts below and the runner
        #    keeps cooling (otherwise the runner would be wound down and
        #    then blink back on, since nothing else wanted its slot).
        deferred: list[float] = []
        now = dt_util.utcnow().timestamp()
        if capacity_frozen:
            # Re-check when the startup grace expires, in case the sensor
            # never reports at all.
            deferred.append(self._boot + TEMP_STARTUP_GRACE)
        if self.max_run > 0 and not self._winddown and len(on) >= limit:
            over = [
                name
                for name in self._rr
                if name in on
                and self._started.get(name) is not None
                and now - self._started[name] >= self.max_run
            ]
            if over:
                rested, resting = self._waiters(on, requesting, now)
                victim: str | None = None
                if rested:
                    best = min(self.zone_priority(w) for w in rested)
                    # Worst-priority over-runner first; ties keep RR order.
                    for name in sorted(
                        over, key=self.zone_priority, reverse=True
                    ):
                        if self.zone_priority(name) >= best:
                            victim = name
                            break
                if victim is not None:
                    _LOGGER.info(
                        "Zone '%s' hit max run time (%s s) with zones waiting;"
                        " rotating it out",
                        victim,
                        self.max_run,
                    )
                    self._preempted[victim] = "max_run"
                    self._schedule_winddown(victim)
                    self._rr.remove(victim)
                    self._rr.append(victim)
                    self._dirty = True
                elif resting:
                    # Every eligible waiter is still resting: re-check when
                    # the first of them has rested long enough.
                    deferred.extend(at for _name, at in resting)

        # 3b) Priority eviction: a rested waiter with strictly higher
        #     priority does not wait for max_run — the worst-priority
        #     runner is rotated out with the same overlap handoff, once it
        #     has run PRIORITY_MIN_RUN seconds (a compressor guard, so a
        #     boost cannot hard-cut a cycle that just started). The rest
        #     gate applies to the evictor, preventing ping-pong. Inert
        #     while a handoff is already in flight, and inert whenever all
        #     priorities are equal.
        if not self._winddown and len(on) >= limit:
            rested, resting = self._waiters(on, requesting, now)
            runners = [n for n in self._rr if n in on]
            if rested:
                best = min(self.zone_priority(w) for w in rested)
                outranked = [
                    n for n in runners if self.zone_priority(n) > best
                ]
                ready = [
                    n
                    for n in outranked
                    if self._started.get(n) is not None
                    and now - self._started[n] >= PRIORITY_MIN_RUN
                ]
                if ready:
                    victim = sorted(
                        ready, key=self.zone_priority, reverse=True
                    )[0]
                    _LOGGER.info(
                        "Zone '%s' (priority %s) rotated out for a"
                        " higher-priority waiter",
                        victim,
                        self.zone_priority(victim),
                    )
                    self._preempted[victim] = "priority"
                    self._schedule_winddown(victim)
                    self._rr.remove(victim)
                    self._rr.append(victim)
                    self._dirty = True
                else:
                    # Outranked runners exist but are still inside the
                    # guard window: re-check when the first clears it.
                    deferred.extend(
                        self._started[n] + PRIORITY_MIN_RUN
                        for n in outranked
                        if self._started.get(n) is not None
                    )
            # A resting waiter that will outrank a runner once rested is
            # worth a re-check at that moment.
            for name, rested_at in resting:
                prio = self.zone_priority(name)
                if any(self.zone_priority(r) > prio for r in runners):
                    deferred.append(rested_at)

        # 4) Temperature shed: when the outdoor temperature lowers the
        #    capacity below what is running, wind the excess down gracefully
        #    (worst priority first, then oldest-started) instead of cutting
        #    power instantly.
        if limit < self.max_zones:
            winding = set(self._winddown)
            countable = on - winding
            excess = len(countable) - limit
            if excess > 0:
                for name in self._shed_order(countable)[:excess]:
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

        # 4b) Outdoor lockout: wind every running zone down, companions
        #     included, through the normal overlap.
        if self._locked_out:
            for name in self._shed_order(on - set(self._winddown)):
                _LOGGER.info("Outdoor lockout; winding down zone '%s'", name)
                self._preempted[name] = "lockout"
                self._schedule_winddown(name)
                self._dirty = True

        winding = set(self._winddown)
        countable = on - winding  # winding zones no longer occupy a slot

        # 5) Manual capacity trim: immediate, worst priority first, then
        #    oldest-started.
        excess = len(countable) - self.max_zones
        if excess > 0:
            for name in self._shed_order(countable)[:excess]:
                _LOGGER.warning(
                    "Capacity trim: switching off zone '%s' (max_zones=%s)",
                    name,
                    self.max_zones,
                )
                await self._turn_off(name)
                countable.discard(name)
                on.discard(name)

        # 6) Global compressor cooldown: once every zone is off, the
        #    compressor must stay off for the minimum off time before
        #    anything may start again. Handoffs are unaffected — while any
        #    zone is on (winding down included) the compressor never
        #    stopped, so this gate only exists when ``on`` is empty.
        now = dt_util.utcnow().timestamp()
        cooldown_block: float | None = None
        if self.cooldown > 0 and not on and self._last_run_end:
            until = max(self._last_run_end.values()) + self.cooldown
            if until > now:
                cooldown_block = until

        # 7) Start waiting zones into free capacity: highest priority
        #    first, round-robin order within a priority (the sort is
        #    stable). The winding-down zones freeing their slot early is
        #    what creates the overlap: the replacement turns on now, the
        #    old zone turns off when its timer fires.
        #    Gates first: the outdoor lockout, then the changeover from the
        #    other direction (only when nothing of ours is on — once this
        #    direction runs, the changeover is behind it).
        changeover, changeover_until = (None, None) if on else self._changeover(now)
        if self._locked_out:
            pass  # lifts on an outdoor reading, which triggers a pass
        elif changeover == "interlock":
            if requesting:
                _LOGGER.debug("Interlock: the other direction is running")
        elif changeover == "delay":
            if requesting:
                deferred.append(changeover_until)
                _LOGGER.debug(
                    "Changeover delay: %.0f s before a zone may start",
                    changeover_until - now,
                )
        elif cooldown_block is not None:
            if requesting:
                deferred.append(cooldown_block)
                _LOGGER.debug(
                    "Compressor cooldown: %.0f s before a zone may start",
                    cooldown_block - now,
                )
        else:
            band_held = False
            for name in sorted(self._rr, key=self.zone_priority):
                effective = len(on) - len(winding)
                if (
                    name not in on
                    and name in requesting
                    and effective < limit
                    # At most one zone of handoff surplus at a time; matches
                    # the original automation's behavior.
                    and len(on) < limit + 1
                ):
                    if changeover == "band" and not on and not self._band_ok(name):
                        band_held = True
                        continue
                    if changeover == "band" and not on:
                        _LOGGER.info(
                            "Zone '%s' changes the system over to %s",
                            name,
                            self.active_word,
                        )
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
                    # Record the start now rather than waiting for the
                    # switch's state-change event, so runtime tracking and
                    # the max-run timer are already correct in this pass.
                    self._started.setdefault(name, dt_util.utcnow().timestamp())
            if band_held and not on:
                # The room temperature may live on another entry's
                # thermostat this one does not track: poll while held.
                deferred.append(now + CHANGEOVER_BAND_RECHECK)

            # 7b) The floor: top up with companions so a running requester
            #     never runs below min_zones (cooldown gating applies, since
            #     a companion only ever starts alongside a requester).
            await self._fill_companions(on, requesting, limit)

        # 8) Keep a timer on every running zone so a reconcile pass fires
        #    the moment it crosses the max-run threshold, and one for the
        #    earliest deferred re-check (rest gate, eviction guard, startup
        #    grace, or cooldown expiry).
        self._update_maxrun_timers(on)
        self._update_rest_timer(min(deferred) if deferred else None)

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
            _LOGGER.info(
                "New %s session started; session total reset", self.active_word
            )
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
        running = bool(on)
        if self._was_running and not running:
            self._wake_opposites()
        self._was_running = running
        # A marker for a zone that is definitively off belongs to a run that
        # ended while HA was down; its tail cannot be recovered, so drop it.
        # Zones still "unavailable" during startup keep their marker until
        # their real state arrives.
        for name in list(self._restored_started):
            state = self.hass.states.get(self._by_name[name].switch_entity)
            if state is not None and state.state == "off":
                del self._restored_started[name]

    # ----------------------------------------------------------- companions

    def _zone_excess(self, name: str) -> tuple[bool, float | None]:
        """Whether the zone may serve as a companion, and how far past its
        setpoint it is (positive = past it, in this entry's direction).

        A zone with a climate entity qualifies only while that thermostat
        is in this entry's mode — a room someone switched off, or left in
        the other mode, is never run for load. A zone without one always
        qualifies, with unknown excess.
        """
        entity = self._by_name[name].climate_entity
        if not entity:
            return True, None
        state = self.hass.states.get(entity)
        if state is None:
            return False, None
        wanted_mode = "heat" if self.mode == MODE_HEATING else "cool"
        # "auto" is dual_smart_thermostat's single-setpoint automatic mode.
        if state.state not in (wanted_mode, "heat_cool", "auto"):
            return False, None
        attrs = state.attributes
        if state.state == "heat_cool":
            key = "target_temp_low" if self.mode == MODE_HEATING else "target_temp_high"
            target = attrs.get(key)
        else:
            target = attrs.get("temperature")
        current = attrs.get("current_temperature")
        try:
            target, current = float(target), float(current)
        except (TypeError, ValueError):
            return True, None
        excess = current - target if self.mode == MODE_HEATING else target - current
        return True, excess

    def _companion_ok(self, name: str, starting: bool) -> bool:
        """May the zone start (or keep) running as a companion?

        Starting needs the room a hysteresis band inside the margin, so a
        zone released at the margin is not picked straight back up.
        """
        eligible, excess = self._zone_excess(name)
        if not eligible:
            return False
        if excess is None:
            return True
        limit = self.companion_margin - (COMPANION_HYSTERESIS if starting else 0.0)
        return excess < limit

    def _companion_rank(self, name: str) -> tuple:
        """Sort key: already on first, then the neediest room, then RR."""
        _eligible, excess = self._zone_excess(name)
        return (
            0 if name in self._winddown else 1,
            excess if excess is not None else float("inf"),
            self._rr.index(name),
        )

    def _floor_target(self, on: set[str], requesting: set[str], limit: int) -> int:
        """How many companions are needed right now (0 when none are)."""
        if self.min_zones <= 1:
            return 0
        winding = set(self._winddown)
        if not (on & requesting) - winding:
            return 0  # no requester is running; nothing to accompany
        floor = min(self.min_zones, limit)
        # Requesters, running or about to start, take precedence.
        return max(0, floor - min(len(requesting), limit))

    def _retain_companions(
        self, on: set[str], requesting: set[str], limit: int
    ) -> None:
        """Drop companions that are off, requesting, unneeded, or past
        their margin. Released zones wind down through step 2."""
        if not self._companions:
            return
        # A companion whose room now asks on its own is a plain requester;
        # one switched off by hand is gone.
        self._companions = {
            c for c in self._companions if c in on and c not in requesting
        }
        needed = self._floor_target(on, requesting, limit)
        keep = sorted(
            (c for c in self._companions if self._companion_ok(c, starting=False)),
            key=self._companion_rank,
        )[:needed]
        for name in sorted(self._companions - set(keep)):
            _LOGGER.info("Companion zone '%s' released", name)
        self._companions = set(keep)

    async def _fill_companions(
        self, on: set[str], requesting: set[str], limit: int
    ) -> None:
        """Start companions until the floor is met or none qualify."""
        needed = self._floor_target(on, requesting, limit)
        short = False
        while len(self._companions) < needed:
            winding = set(self._winddown)
            if len(on - winding) >= limit:
                break
            candidates = [
                z.name
                for z in self.zones
                if z.name not in requesting
                and z.name not in self._companions
                and (z.name not in on or z.name in winding)
                and z.name not in self._preempted
                and self._companion_ok(z.name, starting=True)
            ]
            if not candidates:
                short = True
                break
            name = min(candidates, key=self._companion_rank)
            self._companions.add(name)
            if name in winding:
                # Already running; keep it instead of switching it off.
                self._cancel_winddown(name)
                _LOGGER.info(
                    "Zone '%s' kept on as a companion (min zones %s)",
                    name,
                    self.min_zones,
                )
                continue
            _LOGGER.info(
                "Starting zone '%s' as a companion (min zones %s)",
                name,
                self.min_zones,
            )
            await self._turn_on(name)
            on.add(name)
            self._started.setdefault(name, dt_util.utcnow().timestamp())
        if short and not self._short_of_floor:
            _LOGGER.info(
                "No zone qualifies as a companion; running below min zones (%s)",
                self.min_zones,
            )
        self._short_of_floor = short

    # ------------------------------------------------------ waiter helpers

    def _waiters(
        self, on: set[str], requesting: set[str], now: float
    ) -> tuple[list[str], list[tuple[str, float]]]:
        """Waiting zones in RR order, split by the rest gate.

        Returns rested waiter names, and (name, rested-at) pairs for the
        ones still resting after their last run.
        """
        rested: list[str] = []
        resting: list[tuple[str, float]] = []
        for name in self._rr:
            if name in on or name not in requesting:
                continue
            rested_at = self._rested_at(name)
            if rested_at <= now:
                rested.append(name)
            else:
                resting.append((name, rested_at))
        return rested, resting

    def _shed_order(self, countable: set[str]) -> list[str]:
        """Shed/trim victims: worst priority first, oldest-started within."""
        return sorted(
            (n for n in self._rr if n in countable),
            key=self.zone_priority,
            reverse=True,
        )

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
            "Re-check scheduled in %.0f s (rest gate, eviction guard,"
            " startup grace, compressor cooldown, or changeover)",
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
        fire_at = dt_util.utcnow().timestamp() + self.overlap
        self._winddown[name] = (
            fire_at,
            async_call_later(self.hass, self.overlap, _expired),
        )

    def _cancel_winddown(self, name: str) -> None:
        entry = self._winddown.pop(name, None)
        if entry is not None:
            entry[1]()

    async def _winddown_expired(self, name: str) -> None:
        async with self._lock:
            self._winddown.pop(name, None)
            preempted = name in self._preempted
            self._preempted.pop(name, None)
            zone = self._by_name[name]
            # Only switch off if the request is still gone. If it came back
            # in a race, the zone keeps running. Preempted zones switch off
            # regardless: their request never left, but their turn is over.
            if self._zone_on(name) and (
                preempted
                or (
                    not self._is_on(zone.request_entity)
                    and name not in self._companions
                )
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
        self._pending[name] = ("on", dt_util.utcnow().timestamp())

    async def _turn_off(self, name: str) -> None:
        await self.hass.services.async_call(
            "homeassistant",
            "turn_off",
            {"entity_id": self._by_name[name].switch_entity},
            blocking=True,
        )
        self._pending[name] = ("off", dt_util.utcnow().timestamp())

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
