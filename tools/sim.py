"""Replay the recorded incidents against the manager, no HA install needed.

Stubs the handful of homeassistant modules manager.py imports, models the
~20 s HomeKit confirmation lag on the cooling switches, and asserts the
v1.5.0 behavior: no slot double-booking during switch lag, no blinks at
startup before the outdoor sensor's first reading, and the priority rules
(gated rotation, guarded eviction) acting as designed — while all-default
priorities reproduce the old round-robin exactly. Scenarios L-T cover
the heating floor (v2.1.0): companions, give-back, and their edge cases.

Run with any Python 3.10+: ``python tools/sim.py`` from the repo root.
Scenarios A/A2/F replay real incidents from a live recorder database
(2026-08-12/14/16); the rest exercise the priority rules.
"""
import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(REPO, "custom_components", "hvac_zone_manager")

CURRENT_SIM = None

# ---------------------------------------------------------------- HA stubs


def _identity(func):
    return func


core = types.ModuleType("homeassistant.core")
core.CALLBACK_TYPE = object
core.Event = object
core.HomeAssistant = object
core.callback = _identity

helpers_event = types.ModuleType("homeassistant.helpers.event")


def async_call_later(_hass, delay, cb):
    entry = [CURRENT_SIM.now + delay, cb, True]
    CURRENT_SIM.timers.append(entry)

    def _cancel():
        entry[2] = False

    return _cancel


def async_track_state_change_event(_hass, _entities, _cb):
    return lambda: None


helpers_event.async_call_later = async_call_later
helpers_event.async_track_state_change_event = async_track_state_change_event

helpers_start = types.ModuleType("homeassistant.helpers.start")
helpers_start.async_at_started = lambda _hass, _cb: (lambda: None)

helpers_storage = types.ModuleType("homeassistant.helpers.storage")


class Store:
    def __init__(self, *_a, **_k):
        pass

    async def async_load(self):
        return None

    async def async_save(self, _data):
        pass


helpers_storage.Store = Store

util_dt = types.ModuleType("homeassistant.util.dt")
util_dt.utcnow = lambda: datetime.fromtimestamp(CURRENT_SIM.now, timezone.utc)
util_dt.utc_from_timestamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc)

util = types.ModuleType("homeassistant.util")
util.dt = util_dt

ha = types.ModuleType("homeassistant")
helpers = types.ModuleType("homeassistant.helpers")

for name, mod in {
    "homeassistant": ha,
    "homeassistant.core": core,
    "homeassistant.helpers": helpers,
    "homeassistant.helpers.event": helpers_event,
    "homeassistant.helpers.start": helpers_start,
    "homeassistant.helpers.storage": helpers_storage,
    "homeassistant.util": util,
    "homeassistant.util.dt": util_dt,
}.items():
    sys.modules[name] = mod

# Load const.py and manager.py as a synthetic package (skips __init__.py,
# which drags in config-entry machinery the sim doesn't need).
pkg = types.ModuleType("czm")
pkg.__path__ = [PKG_DIR]
sys.modules["czm"] = pkg
for sub in ("const", "manager"):
    spec = importlib.util.spec_from_file_location(
        f"czm.{sub}", os.path.join(PKG_DIR, f"{sub}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"czm.{sub}"] = mod
    spec.loader.exec_module(mod)

from czm.const import MODE_HEATING  # noqa: E402
from czm.manager import CoolingZoneManager, Zone  # noqa: E402

# ---------------------------------------------------------------- harness

LAG = 20.0  # seconds a switch takes to confirm, per the recorder data


class Sim:
    def __init__(self):
        self.now = 1_000_000.0
        self.timers = []          # [fire_at, cb, alive]
        self.states = {}          # entity_id -> (state, last_changed_ts)
        self.attrs = {}           # entity_id -> attributes (climate only)
        self.physical = []        # [apply_at, entity_id, state]
        self.calls = []           # (service, entity_id, at)
        self.mgr = None
        self.hass = self._make_hass()

    def _make_hass(self):
        sim = self

        class States:
            def get(self, eid):
                if eid not in sim.states:
                    return None
                st, ts = sim.states[eid]
                return SimpleNamespace(
                    state=st,
                    last_changed=datetime.fromtimestamp(ts, timezone.utc),
                    attributes=sim.attrs.get(eid, {}),
                )

        class Services:
            async def async_call(self, _domain, service, data, blocking=True):
                eid = data["entity_id"]
                st = "on" if service == "turn_on" else "off"
                sim.calls.append((service, eid, sim.now))
                sim.physical.append([sim.now + LAG, eid, st])

        class Hass:
            states = States()
            services = Services()

            def async_create_task(self, coro):
                return asyncio.get_event_loop().create_task(coro)

        return Hass()

    def set_state(self, eid, st):
        self.states[eid] = (st, self.now)

    async def tick(self, eid, st):
        """External state change (e.g. temp sensor) + the reconcile it fires."""
        self.set_state(eid, st)
        await self.mgr.async_reconcile()

    async def advance(self, seconds):
        """Move time forward, applying switch confirmations and timers."""
        target = self.now + seconds
        while True:
            due = []
            for p in self.physical:
                if p[0] <= target:
                    due.append((p[0], "p", p))
            for t in self.timers:
                if t[2] and t[0] <= target:
                    due.append((t[0], "t", t))
            if not due:
                break
            due.sort(key=lambda e: e[0])
            at, kind, item = due[0]
            self.now = at
            if kind == "p":
                self.physical.remove(item)
                _, eid, st = item
                if self.states.get(eid, (None, 0))[0] != st:
                    self.set_state(eid, st)
                    await self.mgr.async_reconcile()
            else:
                item[2] = False
                item[1](None)
                await asyncio.sleep(0)  # let created tasks run
                await asyncio.sleep(0)
        self.now = target


ZONES = [
    Zone("LR", "switch.req_lr", "switch.cool_lr"),
    Zone("BR", "switch.req_br", "switch.cool_br"),
    Zone("KO", "switch.req_ko", "switch.cool_ko"),
]


def make_sim(rr, max_run=1200, outdoor="86.2", cooldown=0):
    global CURRENT_SIM
    sim = Sim()
    CURRENT_SIM = sim
    mgr = CoolingZoneManager(
        sim.hass, "test", ZONES, max_zones=3, overlap=30,
        max_run=max_run, temp_entity="sensor.outdoor", cooldown=cooldown,
    )
    mgr.zone_thresholds = {2: 85.0, 3: 70.0}
    mgr._rr = list(rr)
    mgr._boot = sim.now
    sim.mgr = mgr
    for z in ZONES:
        sim.set_state(z.request_entity, "off")
        sim.set_state(z.switch_entity, "off")
    if outdoor is not None:
        sim.set_state("sensor.outdoor", outdoor)
    return sim, mgr


def ons(sim):
    return [c for c in sim.calls if c[0] == "turn_on"]


async def scenario_a():
    """Aug-14 replay: max-run rotation + temp tick in the lag window."""
    sim, mgr = make_sim(["BR", "KO", "LR"])
    for z in ZONES:
        sim.set_state(z.request_entity, "on")
    sim.set_state("switch.cool_lr", "on")
    mgr._started["LR"] = sim.now - 1250
    await mgr.async_reconcile()  # rotation: LR out, BR started
    assert [c[1] for c in ons(sim)] == ["switch.cool_br"], sim.calls
    await sim.advance(5)
    await sim.tick("sensor.outdoor", "86.36")  # the killer pass
    assert [c[1] for c in ons(sim)] == ["switch.cool_br"], (
        "KO was wrongly admitted during the lag window: %s" % sim.calls)
    assert mgr._last_cycle.get("BR") is None, "phantom 0-second cycle recorded"
    await sim.advance(60)  # BR confirms, LR winddown fires and confirms off
    assert sim.states["switch.cool_br"][0] == "on"
    assert sim.states["switch.cool_lr"][0] == "off"
    assert sim.states["switch.cool_ko"][0] == "off"
    assert [c[1] for c in ons(sim)] == ["switch.cool_br"], sim.calls
    print("A: no double-booking during switch lag — PASS")


async def scenario_a2():
    """Aug-12 19:03 replay: all three request at once, limit 1."""
    sim, mgr = make_sim(["BR", "KO", "LR"])
    await sim.tick("switch.req_br", "on")
    await sim.advance(2)
    await sim.tick("switch.req_ko", "on")
    await sim.advance(2)
    await sim.tick("switch.req_lr", "on")
    await sim.advance(3)
    await sim.tick("sensor.outdoor", "86.3")
    await sim.advance(60)
    assert len(ons(sim)) == 1, "over-admission from idle: %s" % sim.calls
    assert not [c for c in sim.calls if c[0] == "turn_off"], sim.calls
    print("A2: mass request start admits exactly one zone — PASS")


async def scenario_b():
    """Priority eviction: P1 waiter evicts a P2 runner immediately."""
    sim, mgr = make_sim(["BR", "KO", "LR"])
    mgr.zone_priorities = {"BR": 1}
    sim.set_state("switch.req_lr", "on")
    sim.set_state("switch.cool_lr", "on")
    mgr._started["LR"] = sim.now - 600  # past the 5-min guard
    await sim.tick("switch.req_br", "on")
    assert mgr._preempted.get("LR") == "priority", mgr._preempted
    assert [c[1] for c in ons(sim)] == ["switch.cool_br"], sim.calls
    await sim.advance(60)
    assert sim.states["switch.cool_lr"][0] == "off"
    assert sim.states["switch.cool_br"][0] == "on"
    print("B: priority eviction with overlap handoff — PASS")


async def scenario_c():
    """The 5-minute guard defers eviction, then the timer completes it."""
    sim, mgr = make_sim(["BR", "KO", "LR"])
    mgr.zone_priorities = {"BR": 1}
    sim.set_state("switch.req_lr", "on")
    sim.set_state("switch.cool_lr", "on")
    mgr._started["LR"] = sim.now - 100  # inside the guard
    await sim.tick("switch.req_br", "on")
    assert not ons(sim), "evicted inside the compressor guard: %s" % sim.calls
    assert mgr._rest_timer is not None, "no re-check scheduled"
    await sim.advance(210)  # guard clears at +200; timer at +201
    assert mgr._preempted.get("LR") == "priority", mgr._preempted
    assert [c[1] for c in ons(sim)] == ["switch.cool_br"], sim.calls
    print("C: compressor guard defers, timer completes eviction — PASS")


async def scenario_d():
    """A P1 runner is never max-run rotated for a P2 waiter."""
    sim, mgr = make_sim(["LR", "KO", "BR"])
    mgr.zone_priorities = {"BR": 1}
    sim.set_state("switch.req_br", "on")
    sim.set_state("switch.cool_br", "on")
    mgr._started["BR"] = sim.now - 1500  # well past max_run
    await sim.tick("switch.req_lr", "on")
    assert "BR" not in mgr._preempted, "P1 rotated out for a P2 waiter"
    assert not ons(sim), sim.calls
    # Equal priority makes it rotate again:
    mgr.zone_priorities = {"BR": 1, "LR": 1}
    await mgr.async_reconcile()
    assert mgr._preempted.get("BR") == "max_run", mgr._preempted
    assert [c[1] for c in ons(sim)] == ["switch.cool_lr"], sim.calls
    print("D: max-run rotation is priority-gated — PASS")


async def scenario_e():
    """All defaults: the classic rotation still behaves exactly as before."""
    sim, mgr = make_sim(["LR", "KO", "BR"])
    sim.set_state("switch.req_br", "on")
    sim.set_state("switch.cool_br", "on")
    mgr._started["BR"] = sim.now - 1500
    await sim.tick("switch.req_lr", "on")
    assert mgr._preempted.get("BR") == "max_run", mgr._preempted
    assert [c[1] for c in ons(sim)] == ["switch.cool_lr"], sim.calls
    # Rotation sends BR to the back, then starting LR sends LR behind it.
    assert mgr._rr == ["KO", "BR", "LR"], mgr._rr
    await sim.advance(60)
    assert sim.states["switch.cool_br"][0] == "off"
    assert sim.states["switch.cool_lr"][0] == "on"
    print("E: default round-robin rotation unchanged — PASS")


async def scenario_f():
    """Today's restart replay: sensor not loaded yet, one zone running."""
    sim, mgr = make_sim(["BR", "KO", "LR"], outdoor=None)
    for z in ZONES:
        sim.set_state(z.request_entity, "on")
    sim.set_state("switch.cool_br", "on")
    await mgr.async_reconcile()  # initial pass, sensor unreadable
    assert not sim.calls, "capacity changed before first reading: %s" % sim.calls
    await sim.tick("sensor.outdoor", "100.2")  # first reading: 1 zone allowed
    assert not sim.calls, "blink on first reading: %s" % sim.calls
    assert sim.states["switch.cool_br"][0] == "on"
    print("F: startup freeze holds one running zone, no shed — PASS")


async def scenario_g():
    """Startup freeze from idle still lets one zone begin cooling."""
    sim, mgr = make_sim(["BR", "KO", "LR"], outdoor=None)
    for z in ZONES:
        sim.set_state(z.request_entity, "on")
    await mgr.async_reconcile()
    assert len(ons(sim)) == 1, sim.calls
    await sim.advance(60)
    assert len(ons(sim)) == 1, sim.calls
    print("G: frozen startup admits exactly one zone from idle — PASS")


async def scenario_h():
    """Sensor never reports: grace expires, manual max takes over."""
    sim, mgr = make_sim(["BR", "KO", "LR"], outdoor=None)
    for z in ZONES:
        sim.set_state(z.request_entity, "on")
    await mgr.async_reconcile()
    assert len(ons(sim)) == 1, sim.calls
    await sim.advance(360)  # grace is 300 s; the deferred timer fires
    assert len(ons(sim)) == 3, (
        "manual-max fail-safe did not take over: %s" % sim.calls)
    print("H: dead sensor falls back to manual max after grace — PASS")


async def scenario_i():
    """Compressor cooldown: a start after all-off waits out the minimum."""
    sim, mgr = make_sim(["BR", "KO", "LR"], cooldown=300)
    await sim.tick("switch.req_br", "on")
    assert [c[1] for c in ons(sim)] == ["switch.cool_br"], (
        "first-ever start should not be cooldown-blocked: %s" % sim.calls)
    await sim.advance(600)  # BR confirms and runs
    await sim.tick("switch.req_br", "off")  # winddown starts (30 s)
    await sim.advance(120)  # off commanded at +30, confirmed at +50
    assert sim.states["switch.cool_br"][0] == "off"
    # 90 s after the off command: still inside the 300 s cooldown.
    await sim.tick("switch.req_br", "on")
    assert len(ons(sim)) == 1, "start during cooldown: %s" % sim.calls
    assert mgr.zone_status("BR") == "cooldown", mgr.zone_status("BR")
    cd = mgr.zone_countdown("BR")
    assert cd is not None and cd[1] == "compressor_cooldown", cd
    assert mgr._rest_timer is not None, "no re-check scheduled for cooldown end"
    await sim.advance(400)  # cooldown expires; deferred timer starts BR
    assert len(ons(sim)) == 2, "cooldown never released: %s" % sim.calls
    assert sim.states["switch.cool_br"][0] == "on"
    print("I: compressor cooldown blocks, then releases the start — PASS")


async def scenario_j():
    """Cooldown never blocks an overlap handoff (compressor stays on)."""
    sim, mgr = make_sim(["LR", "KO", "BR"], cooldown=600)
    sim.set_state("switch.req_br", "on")
    sim.set_state("switch.cool_br", "on")
    mgr._started["BR"] = sim.now - 1500
    await sim.tick("switch.req_lr", "on")
    assert mgr._preempted.get("BR") == "max_run", mgr._preempted
    assert [c[1] for c in ons(sim)] == ["switch.cool_lr"], (
        "handoff start wrongly cooldown-blocked: %s" % sim.calls)
    await sim.advance(60)
    assert sim.states["switch.cool_br"][0] == "off"
    assert sim.states["switch.cool_lr"][0] == "on"
    print("J: overlap handoff unaffected by the cooldown — PASS")


async def scenario_k():
    """Free capacity: an over-limit runner is NOT rotated for a new waiter."""
    sim, mgr = make_sim(["LR", "KO", "BR"], outdoor="60.0")  # tier 3 allowed
    sim.set_state("switch.req_br", "on")
    sim.set_state("switch.cool_br", "on")
    mgr._started["BR"] = sim.now - 1500  # past max_run
    await sim.tick("switch.req_lr", "on")
    assert "BR" not in mgr._preempted, (
        "rotated a runner although the waiter had a free slot")
    assert [c[1] for c in ons(sim)] == ["switch.cool_lr"], sim.calls
    await sim.advance(120)
    assert sim.states["switch.cool_br"][0] == "on", "BR blinked off"
    assert sim.states["switch.cool_lr"][0] == "on"
    print("K: no rotation while free capacity exists, no blink — PASS")


# ------------------------------------------------------------- heating

HEAT_ZONES = [
    Zone("LR", "switch.hreq_lr", "switch.heat_lr", "climate.lr"),
    Zone("BR", "switch.hreq_br", "switch.heat_br", "climate.br"),
    Zone("KO", "switch.hreq_ko", "switch.heat_ko", "climate.ko"),
]


def make_heat_sim(rr=("LR", "BR", "KO"), min_zones=2, cooldown=0):
    global CURRENT_SIM
    sim = Sim()
    CURRENT_SIM = sim
    mgr = CoolingZoneManager(
        sim.hass, "heat", HEAT_ZONES, max_zones=3, overlap=30,
        cooldown=cooldown, mode=MODE_HEATING, min_zones=min_zones,
        companion_margin=2.0,
    )
    mgr._rr = list(rr)
    mgr._boot = sim.now
    sim.mgr = mgr
    for z in HEAT_ZONES:
        sim.set_state(z.request_entity, "off")
        sim.set_state(z.switch_entity, "off")
        room(sim, z.name, 68.0)
    return sim, mgr


def room(sim, name, current, target=68.0, mode="heat"):
    eid = "climate." + name.lower()
    sim.set_state(eid, mode)
    sim.attrs[eid] = {"temperature": target, "current_temperature": current}


async def room_tick(sim, name, current, target=68.0, mode="heat"):
    room(sim, name, current, target, mode)
    await sim.mgr.async_reconcile()


def on_now(sim):
    return sorted(z.name for z in HEAT_ZONES if sim.mgr._zone_on(z.name))


async def scenario_l():
    """One zone calls for heat: the neediest other zone joins it."""
    sim, mgr = make_heat_sim()
    room(sim, "LR", 67.5)
    room(sim, "KO", 66.0)  # furthest below setpoint
    room(sim, "BR", 66.5)
    await sim.tick("switch.hreq_br", "on")
    assert [c[1] for c in ons(sim)] == ["switch.heat_br", "switch.heat_ko"], sim.calls
    assert mgr.companion_zones == ["KO"], mgr.companion_zones
    assert mgr.zone_status("KO") == "companion"
    assert mgr.zone_status("BR") == "heating"
    await sim.advance(120)  # both confirm; nothing winds down
    assert on_now(sim) == ["BR", "KO"], on_now(sim)
    assert not [c for c in sim.calls if c[0] == "turn_off"], sim.calls
    print("L: lone heat call pulls in the neediest companion - PASS")


async def scenario_m():
    """Give-back: a companion past its margin hands off with overlap."""
    sim, mgr = make_heat_sim()
    room(sim, "KO", 66.0)
    room(sim, "LR", 67.0)
    await sim.tick("switch.hreq_br", "on")
    await sim.advance(60)
    assert mgr.companion_zones == ["KO"]
    await room_tick(sim, "KO", 69.9)  # +1.9: still inside the 2.0 margin
    assert mgr.companion_zones == ["KO"], "released inside the margin"
    await room_tick(sim, "KO", 70.1)  # +2.1: past it
    assert mgr.companion_zones == ["LR"], mgr.companion_zones
    assert "KO" in mgr.winding_down, "KO should hand off through overlap"
    await sim.advance(90)
    assert on_now(sim) == ["BR", "LR"], on_now(sim)
    # Back to +1.6: inside the margin but not the hysteresis band, so KO
    # may not be picked again yet.
    room(sim, "KO", 69.6)
    assert not mgr._companion_ok("KO", starting=True)
    print("M: companion past its margin hands off to the next - PASS")


async def scenario_n():
    """A second real call makes the companion unnecessary."""
    sim, mgr = make_heat_sim()
    room(sim, "KO", 66.0)
    await sim.tick("switch.hreq_br", "on")
    await sim.advance(60)
    await sim.tick("switch.hreq_lr", "on")
    assert "LR" in on_now(sim)
    assert mgr.companion_zones == [], mgr.companion_zones
    assert "KO" in mgr.winding_down
    await sim.advance(90)
    assert on_now(sim) == ["BR", "LR"], on_now(sim)
    print("N: a real second call releases the companion - PASS")


async def scenario_o():
    """The requester is satisfied: everything stops, cooldown holds."""
    sim, mgr = make_heat_sim(cooldown=300)
    room(sim, "KO", 66.0)
    await sim.tick("switch.hreq_br", "on")
    await sim.advance(600)
    await sim.tick("switch.hreq_br", "off")
    assert mgr.companion_zones == [], mgr.companion_zones
    assert set(mgr.winding_down) == {"BR", "KO"}, mgr.winding_down
    await sim.advance(120)
    assert on_now(sim) == [], on_now(sim)
    n = len(ons(sim))
    await sim.tick("switch.hreq_lr", "on")  # inside the cooldown
    assert len(ons(sim)) == n, "start during furnace cooldown: %s" % sim.calls
    await sim.advance(400)
    assert on_now(sim) == ["KO", "LR"], on_now(sim)
    print("O: satisfied requester stops both; cooldown then pairs again - PASS")


async def scenario_p():
    """A companion whose own room starts asking stays on, no blink."""
    sim, mgr = make_heat_sim()
    room(sim, "KO", 66.0)
    await sim.tick("switch.hreq_br", "on")
    await sim.advance(60)
    await sim.tick("switch.hreq_ko", "on")
    assert mgr.companion_zones == [], mgr.companion_zones
    assert mgr.zone_status("KO") == "heating"
    assert not mgr.winding_down, mgr.winding_down
    await sim.advance(120)
    assert on_now(sim) == ["BR", "KO"], on_now(sim)
    assert not [c for c in sim.calls if c[0] == "turn_off"], sim.calls
    print("P: companion that starts requesting just keeps running - PASS")


async def scenario_q():
    """No eligible companion: the requester heats alone."""
    sim, mgr = make_heat_sim()
    room(sim, "KO", 66.0, mode="off")   # switched off: never pulled in
    room(sim, "LR", 66.0, mode="cool")  # wrong mode: never pulled in
    await sim.tick("switch.hreq_br", "on")
    assert [c[1] for c in ons(sim)] == ["switch.heat_br"], sim.calls
    assert mgr.short_of_floor
    # Turning LR to heat makes it eligible; it joins on that change.
    await room_tick(sim, "LR", 66.0, mode="heat")
    assert mgr.companion_zones == ["LR"], mgr.companion_zones
    assert not mgr.short_of_floor
    print("Q: no eligible zone -> heats alone; joins when one qualifies - PASS")


async def scenario_r():
    """A companion's thermostat switched off mid-run releases it."""
    sim, mgr = make_heat_sim()
    room(sim, "KO", 66.0)
    room(sim, "LR", 70.0)  # +2.0: not eligible to start
    await sim.tick("switch.hreq_br", "on")
    await sim.advance(60)
    await room_tick(sim, "KO", 66.0, mode="off")
    assert mgr.companion_zones == [], mgr.companion_zones
    assert "KO" in mgr.winding_down
    print("R: companion released when its thermostat is turned off - PASS")


async def scenario_s():
    """Restart mid-run: an on, non-requesting zone is re-adopted."""
    sim, mgr = make_heat_sim()
    room(sim, "KO", 66.0)
    sim.set_state("switch.hreq_br", "on")
    sim.set_state("switch.heat_br", "on")
    sim.set_state("switch.heat_ko", "on")  # companion before the restart
    await mgr.async_reconcile()
    assert mgr.companion_zones == ["KO"], mgr.companion_zones
    assert not mgr.winding_down, mgr.winding_down
    assert not sim.calls, sim.calls
    print("S: restart re-adopts the running companion, no switching - PASS")


async def scenario_t():
    """min_zones 0 (every cooling entry): never a companion."""
    sim, mgr = make_heat_sim(min_zones=0)
    room(sim, "KO", 60.0)
    await sim.tick("switch.hreq_br", "on")
    assert [c[1] for c in ons(sim)] == ["switch.heat_br"], sim.calls
    assert not mgr.short_of_floor
    print("T: floor off -> plain behavior - PASS")


async def main():
    for scen in (scenario_a, scenario_a2, scenario_b, scenario_c,
                 scenario_d, scenario_e, scenario_f, scenario_g,
                 scenario_h, scenario_i, scenario_j, scenario_k,
                 scenario_l, scenario_m, scenario_n, scenario_o,
                 scenario_p, scenario_q, scenario_r, scenario_s,
                 scenario_t):
        await scen()
    print("\nall scenarios passed")


asyncio.run(main())
