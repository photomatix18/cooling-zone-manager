# Changelog

All notable changes to the HVAC Zone Manager integration (named Cooling Zone
Manager before 2.0.0) are documented here.

## 2.2.0 — 2026-10-19

### Added — outdoor lockout and heat/cool changeover

Prompted by a night with the thermostats in auto (single 70 °F setpoint,
±0.3 °F tolerance): the furnace's overshoot tipped every room into a cooling
call, the AC overcooled it back into a heating call, and the house swapped
directions about once an hour until morning — with no gap between furnace off
and AC on, at 54–58 °F outside.

- **Cooling lockout below** (cooling entry, default 60 °F / 15.5 °C) and
  **Heating lockout above** (heating entry, default 65 °F / 18.5 °C), when the
  entry has the outdoor sensor. Crossing the line winds running zones down;
  1 °F of hysteresis on the way back. New zone status **`lockout`**.
- **Interlock**: a cooling and a heating entry never run at the same time.
- **Changeover delay** (default 30 min): a direction may not start until the
  other has been off this long.
- **Changeover band** (default 2 °F / 1 °C): after the delay, a zone whose
  thermostat is in `auto`/`heat_cool` only changes the system over once its
  room is this far past setpoint. Explicit heat/cool modes skip it.
- New zone status **`changeover`**, countdown type `changeover`, and Active
  zones attributes `lockout_temp`, `locked_out`, `changeover`,
  `changeover_until`, `changeover_delay_seconds`, `changeover_band`.
- A cooling zone without a thermostat borrows the same-named heating zone's.
- `tools/sim.py` scenarios U–Z, including a replay of the night above.

### Changed

- A heating entry with an outdoor sensor no longer gets the *Allow N zones
  below* capacity thresholds (cooling-only concept); it gets the lockout.
- Thermostats in dual_smart_thermostat's single-setpoint **`auto`** mode now
  qualify as heating companions (before, only `heat`/`heat_cool` did, so in
  auto the floor never found a companion).

All 21 earlier simulation scenarios pass unchanged.

## 2.1.0 — 2026-10-16

### Added — heating mode

- **Mode** on the first setup screen: an entry manages either cooling or
  heating. Entries made before 2.1.0 have no mode and stay cooling, unchanged.
- **Min zones** (heating entries, default `2`, `0`/`1` = off): when a zone
  calls for heat and fewer zones are running, the manager pulls in a
  **companion** — the zone furthest below its own setpoint whose thermostat
  is in heat mode — so the furnace never runs one zone alone.
- **Companion overshoot** (heating entries, default 2 °F / 1 °C): a companion
  gives its slot back once its room passes setpoint + this margin, handing
  off through the normal overlap; half a degree of hysteresis keeps it from
  being picked straight back up. It also steps aside for a second real call,
  when the requester is satisfied, or when its own room starts asking.
- If no zone qualifies, the requester runs alone and the Active zones sensor
  shows `short_of_min_zones: true` — the floor never keeps a room cold.
- An optional **thermostat** (climate entity) per zone, in the setup and
  add-zone forms. Companions need it; cooling doesn't use it.
- New status **`companion`**; zone status reads **`heating`** on a heating
  entry. The Active zones sensor gained `mode`, `min_zones`, `companions` and
  `short_of_min_zones`; zone status sensors gained `companion` and
  `climate_entity`. The cooldown number is named **Furnace cooldown** on a
  heating entry.
- `tools/sim.py` scenarios L–T cover the floor.

Cooling entries behave exactly as in 2.0.0 (all twelve existing simulation
scenarios pass unchanged).

## 2.0.0 — 2026-10-16

### Breaking — renamed to HVAC Zone Manager

The integration started cooling-only and is headed for heating as well, so it
is renamed. **The domain changes from `cooling_zone_manager` to
`hvac_zone_manager`**, and Home Assistant has no migration path for a domain
change — this is a delete-and-re-add:

1. Note the current values of every number entity (max zones, overlap, max run
   time, compressor cooldown, priorities, temperature thresholds).
2. Copy `custom_components/hvac_zone_manager` in alongside the old folder and
   restart.
3. Add **HVAC Zone Manager** from Devices & Services, re-enter the zones and
   settings, and set the numbers back to what they were.
4. Delete the old **Cooling Zone Manager** entry, remove
   `custom_components/cooling_zone_manager`, and restart again.
5. Repoint dashboards and automations: every entity ID changes
   (`…cooling_zone_manager_…` → `…<new entry title>_…`), recorder history
   starts fresh, and the stored round-robin order and runtime totals reset.
   The logger key becomes `custom_components.hvac_zone_manager`.

Nothing else changed in this release — behavior is identical to 1.6.0. The
GitHub repository moved to `photomatix18/hvac-zone-manager` (the old URL
redirects).

## 1.6.0 — 2026-08-17

### Added

- **Compressor cooldown (anti-short-cycle).** A new **Compressor cooldown**
  number entity (and config-flow field): once every zone is off, no zone may
  start again until this many minutes have passed since the last zone
  switched off — so the compressor can never stop and immediately restart.
  Default 5 minutes, `0` disables it. Overlap handoffs are unaffected: while
  any zone is still on (winding down included) the compressor never stopped,
  so zone-to-zone rotation keeps working exactly as before. The cooldown
  survives restarts (it is derived from the persisted run-end times), so a
  Home Assistant reboot right after the system shuts off still respects it.
  A future heating manager entry gets its own independent knob — set either
  one to `0` where no cooldown is wanted.

- **Every delay is now visible.** Anything that makes the manager *wait* —
  which used to look like a bug from the dashboard — now shows as a live
  countdown:
  - A **Cooldown ends** timestamp sensor: when the compressor cooldown
    expires (unknown while none is running), with the configured length and
    the moment the compressor went off as attributes.
  - A per-zone **countdown** timestamp sensor showing the zone's next
    scheduled transition, with a `countdown_type` attribute naming it:
    `switching_off` (wind-down ends), `compressor_cooldown`, `resting`
    (its own rest gate opens), `priority_eviction` / `waits_for_guard`
    (the 5-minute compressor guard clears), and `max_run_rotation` /
    `waits_for_rotation` (a runner reaches the max-run limit). Timestamp
    sensors render as live countdowns ("in 4 minutes") on any dashboard
    card.
  - Zone status sensors gained a **`cooldown`** state, shown while a
    requesting zone is blocked only by the compressor cooldown, plus
    `winddown_until`, `resting_until`, and the countdown as attributes.
  - The Active zones sensor now exposes `cooldown_seconds` and
    `cooldown_until`.

### Fixed

- **A zone is no longer rotated out when the new waiter has a free slot.**
  The max-run rotation fired whenever a runner was over the limit and a
  rested zone was waiting — even if capacity was free, so the waiter could
  simply have started. The runner was wound down for nothing, blinked off,
  and restarted into the freed capacity moments later. Rotation now only
  happens when the waiter genuinely has no free slot to take; with capacity
  to spare, the waiter starts and the runner keeps cooling (a zone with no
  competition was never supposed to be cut off). With one allowed zone —
  the common hot-day case — nothing changes.

## 1.5.0 — 2026-08-16

### Fixed

- **Zones no longer blink on and then off when a slot changes hands.** The
  cooling switches (template switches backed by a thermostat over HomeKit)
  can take ~20 seconds to report a state change back. The reconciler trusted
  the observed state, so a pass that ran inside that window saw the slot it
  had just filled as still empty, admitted a *second* zone into it, and the
  over-capacity shed then switched the first zone off ~50 seconds after it
  started — consuming its round-robin turn, so it went to the back of the
  queue and waited out a full rotation. In a 3-zone install this happened
  several times a day, worst at night when every zone was requesting.

  The manager now remembers its own unconfirmed turn-on/off commands and
  trusts them over the observed switch state until the switch confirms or
  60 seconds pass (then it logs a warning and the observed state rules
  again, so self-healing after manual flips and restarts is unchanged).
  This also stops phantom 0-second cycles from polluting the cycle-runtime
  sensors and the rest gate.

- **No more zone blinks on restart.** The first reconcile after a restart
  could run before the outdoor temperature sensor's integration had loaded,
  so capacity briefly fell back to the manual max, every requesting zone
  started, and the sensor's first reading seconds later shed the extras.
  Now, until a configured sensor delivers its first reading (up to 5
  minutes), capacity changes are frozen: running zones keep running,
  nothing extra starts, nothing is shed — from idle, one zone may still
  begin cooling. If the sensor stays silent past the grace, the manual Max
  zones applies as before. A mid-season sensor dropout now also keeps the
  last known limit instead of snapping to full capacity and back.

### Added

- **Zone priorities.** Each zone gets a **priority** number entity: `1` runs
  first, `3` yields to everyone, default `2`. Set them from automations
  (bedtime, cooking, movie night), a dashboard card, or voice — the manager
  just obeys the current values:
  - Free capacity goes to the highest-priority waiter first; round-robin
    order breaks ties.
  - A runner is only max-run rotated for a waiter of its own priority or
    better — a boosted zone runs as long as it wants while lesser zones
    share the leftover capacity.
  - A rested waiter with *strictly higher* priority doesn't wait for the
    max-run rotation: the worst-priority runner is wound down with the
    normal overlap handoff once it has run at least 5 minutes (a compressor
    guard so a boost can't hard-cut a fresh cycle).
  - Temperature sheds and capacity trims take the worst-priority zones
    first.
  - The Active zones sensor and each per-zone status sensor expose the
    current priority.

  With every zone left at `2` the scheduler behaves exactly as 1.4.2 did.

## 1.4.2 — 2026-07-03

### Fixed

- **Max-run rotation no longer lets a zone instantly reclaim its slot.**
  When the running zones' clocks were in sync (e.g. both started when the
  cooling session began) and one hit the max run time, the other was also
  over the limit — and the moment the rotated-out zone finished its overlap
  wind-down, it became the only "waiting" zone, immediately evicted the
  other long-runner (whose run clock had legitimately never reset through
  the handoff), and re-seated itself with a zero-second off blink. The
  visible symptoms: rotations arriving in pairs one overlap apart, a zone
  keeping its slot for only the overlap time after a rotation, switches
  blinking off/on within a second, and cycle-runtime sensors resetting
  mid-run.

  Now a zone that just finished a run must **rest** — stay off for as long
  as its last run lasted, capped at the max run time — before it can force
  another zone out. It can still take naturally-freed capacity immediately
  (a zone that shuts off because its room is satisfied is unaffected). With
  three zones and two slots this converges to clean single rotations: each
  zone cools in one continuous stretch, then rests its fair share. All
  other configurations behave exactly as before.

### Added

- The **Active zones** sensor now lists `resting` zones (waiting zones that
  can't force a rotation yet) so you can see the rotation logic's state at
  a glance.

## 1.4.1 — 2026-07-03

### Added

- **Integration icon.** A "zoned house" icon now ships with the integration
  (in its `brand/` folder) and appears in the Home Assistant UI — Settings →
  Devices & Services, config dialogs, and HACS — on Home Assistant 2026.3 or
  newer, which serves local brand images automatically. On older versions
  the icon simply doesn't show; nothing else changes.

## 1.4.0 — 2026-07-03

### Added

- **Options flow — reconfigure without re-adding.** Settings → Devices &
  Services → Cooling Zone Manager → **Configure** now offers:
  - **Outdoor temperature sensor**: pick, change, or clear it. This is how
    existing installs enable the temperature-aware capacity from 1.3.0 —
    no more delete-and-re-add.
  - **Add a zone**: joins the rotation immediately.
  - **Remove zones**: removed zones are switched off first if they're
    cooling, and their leftover sensor entities are cleaned up. At least
    one zone must remain.
  - Changes apply immediately (the integration reloads itself), and all
    tuned values (max zones, overlap, max run time, thresholds, runtime
    history) are preserved.

## 1.3.0 — 2026-07-03

### Added

- **Temperature-aware capacity.** Pick an optional outdoor temperature sensor
  in the config flow and the integration limits how many zones may run at
  once based on the heat outside:
  - One threshold number entity per zone beyond the first — e.g. with three
    zones you get **Allow 2 zones below** (default 85 °F / 29 °C) and
    **Allow 3 zones below** (default 75 °F / 24 °C). Tune them any time.
  - The effective limit is the smaller of the manual **Max zones** and what
    the temperature allows, so Max zones stays your hard ceiling.
  - **1° hysteresis**: the reading must cross a threshold by a full degree
    before capacity changes, so a temperature hovering at the boundary
    doesn't flap zones on and off.
  - When the temperature *lowers* capacity below what's running, excess
    zones wind down gracefully with the normal overlap handoff (oldest
    first) instead of being cut off instantly.
  - If the sensor is unavailable or no sensor is configured, the manual
    Max zones applies unchanged.
  - The Active zones sensor now shows `outdoor_temp`, `temp_allowed_zones`,
    and `effective_max_zones` attributes so you can see why the manager
    chose its current limit.

## 1.2.0 — 2026-07-03

### Changed

- **Runtime sensors are now cycle-based** instead of lifetime totals:
  - Each zone's runtime sensor (now named *cycle runtime*) shows how long the
    zone's **current run** has been cooling. It resets to zero the moment the
    zone starts a run, and holds the last run's duration while the zone is off
    (a `running` attribute tells you which you're looking at).
  - The total sensor (now named *Session runtime*) accumulates cooling time
    across all zones for the **current cooling session**. Once every zone is
    satisfied and switched off, the next request resets it to zero and starts
    a new session (a `session_started` attribute shows when).
  - Because these values reset, the sensors' state class changed from
    `total_increasing` to `measurement`. If you had created statistics or
    utility meters on the 1.1.0 sensors, remove or re-create them.
- **Max zone run time is now set in minutes** (config flow and number
  entity). A value stored in seconds by 1.1.0 is converted automatically on
  upgrade — no action needed.

## 1.1.0 — 2026-07-03

### Added

- **Version display** — the installed version now shows on the device page in
  Home Assistant (Settings → Devices & Services → Cooling Zone Manager) and as
  a `version` attribute on the Active zones sensor, so you can confirm an
  update actually took effect.
- **Per-zone status sensors** — one sensor per zone showing `cooling`,
  `winding_down`, `waiting`, or `idle`, with attributes for the request state,
  the actual switch state, when the current run started, and total runtime.
- **Runtime tracking** — a runtime sensor per zone plus a Total runtime sensor
  for the whole system. Totals persist across restarts and reloads, and are
  statistics-friendly (`total_increasing`), so you can wrap them in utility
  meter helpers for daily/weekly/monthly runtime.
- **Max zone run time** — a new tunable (config flow field + number entity).
  If a zone has been cooling this long *and other zones are waiting*, it is
  rotated out with the normal overlap handoff and sent to the back of the
  round-robin queue, so one stubborn zone can't hog the capacity. A zone with
  no competition is never cut off. Set to `0` to disable (the default, so
  existing installs behave exactly as before).
- **Richer Active zones attributes** — the sensor now also lists preempted
  zones, the max run setting, and a per-zone detail map (status, request,
  switch, current run, total runtime).
- This changelog.

### Changed

- Nothing in existing behavior. Round-robin fairness, overlap handoff,
  anti-short-cycle re-admission, and capacity trimming all work as in 1.0.0.

### Upgrade notes

- Existing config entries keep working without reconfiguration. The new
  **Max zone run time** number entity appears automatically (default `0` =
  disabled) — set it from the device page whenever you're ready.

## 1.0.0

- Initial release: UI-configured cooling zone arbiter with round-robin
  fairness, overlap handoff, anti-short-cycle protection, capacity trimming,
  self-healing reconciliation, Max zones and Overlap time number entities,
  and the Active zones status sensor.
