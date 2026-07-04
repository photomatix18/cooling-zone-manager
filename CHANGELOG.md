# Changelog

All notable changes to the Cooling Zone Manager integration are documented here.

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
