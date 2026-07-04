<p align="center">
  <img src="assets/icon.png" width="128" alt="Cooling Zone Manager icon">
</p>

# Cooling Zone Manager

A custom Home Assistant integration that lets **at most N cooling zones run at the same time**, with round-robin fairness and an overlap window when one zone hands off to the next. Everything is configured in the Home Assistant UI — no YAML, no automations, no helpers to create.

## What it does

- A zone starts cooling as soon as its **request entity** turns on, if capacity allows.
- Fairness is **round-robin**: when there are more requests than slots, the zone that has waited longest gets the next free slot.
- When a zone's request turns off, the next waiting zone starts **immediately**, and the old zone keeps running for the **overlap time** before shutting off (a smooth handoff).
- If the old zone's request comes back during the overlap, it just keeps running — no short-cycling.
- If a zone has been cooling longer than the **max zone run time** while other zones are waiting, it gets rotated out (same smooth handoff) and goes to the back of the queue — one stubborn zone can't hog the capacity. Set it to `0` to disable.
- Lowering **Max zones** trims the oldest-running zones immediately.
- Optionally, an **outdoor temperature sensor** scales the capacity automatically — e.g. only 1 zone above 85°, 2 below 85°, all 3 below 75° — with hysteresis so a hovering reading doesn't flap zones. See below.
- **Runtime is tracked** per cycle: each zone shows how long its current run has been cooling, and a session total accumulates across zones until everything is satisfied — then the next request starts a fresh session. Survives restarts.
- The manager re-derives everything from live entity states, so it self-heals after Home Assistant restarts, reloads, or manual switch flips. (A zone switched on by hand with no request will be wound down after the overlap time.)

## Entities it creates

One device ("Cooling Zone Manager") showing the installed **version**, with these entities:

| Entity | Purpose |
|---|---|
| `number.cooling_zone_manager_max_zones` | Max zones allowed at once — change it any time |
| `number.cooling_zone_manager_overlap_time` | Overlap / wind-down time in seconds |
| `number.cooling_zone_manager_max_zone_run_time` | Longest a zone may cool while others wait, in minutes (`0` = no limit) |
| `sensor.cooling_zone_manager_active_zones` | How many zones are cooling now; attributes show active / requesting / winding-down / waiting / preempted zones, the round-robin order, a per-zone detail map, and the version |
| `sensor.cooling_zone_manager_<zone>_status` | One per zone: `cooling`, `winding_down`, `waiting`, or `idle`, with the request state, actual switch state, and cycle runtime as attributes |
| `sensor.cooling_zone_manager_<zone>_runtime` | One per zone: how long the **current run** has been cooling. Resets when the zone starts; holds the last run's duration while off (`running` attribute says which) |
| `sensor.cooling_zone_manager_total_runtime` | Cooling time across all zones for the **current session**. Resets when a zone requests again after everything was satisfied |
| `number.cooling_zone_manager_allow_2_zones_below` etc. | Only when an outdoor temperature sensor is configured: one threshold per zone beyond the first (see below) |

The numbers remember their values across restarts, so they replace the old `input_number` helpers. The runtime tracking persists across restarts too.

## Temperature-aware capacity (optional)

If your AC can't keep up with several zones on a hot day, pick an **outdoor temperature sensor** when adding the integration (or add one later via **Configure** on the integration entry). You then get one threshold number per zone beyond the first — with three zones:

| Entity | Meaning | Default |
|---|---|---|
| **Allow 2 zones below** | Outdoor temp below which 2 zones may run | 85 °F / 29 °C |
| **Allow 3 zones below** | Outdoor temp below which all 3 may run | 75 °F / 24 °C |

Above the first threshold only 1 zone runs at a time. The temperature limit combines with **Max zones** by taking the smaller of the two, so Max zones remains your hard ceiling.

Details that keep it smooth:

- **Hysteresis (1°):** the reading must cross a threshold by a full degree before capacity changes, so 84.9° → 85.1° → 84.9° doesn't flap zones on and off.
- **Graceful shedding:** when a rising temperature lowers the capacity below what's running, the excess zones (oldest first) wind down through the normal overlap instead of being switched off instantly, and go to the back of the round-robin queue.
- **Fail-safe:** if the sensor goes unavailable, the manual Max zones applies unchanged.
- The Active zones sensor shows `outdoor_temp`, `temp_allowed_zones`, and `effective_max_zones` attributes so you can always see why the manager chose its current limit.

## Max zone run time (fairness limit)

Sometimes one zone can't reach its setpoint — an oversized room, a heat wave, a wide-open door — and would otherwise occupy a cooling slot forever while other zones wait. When **Max zone run time** is set (in minutes, e.g. `60` = 1 hour):

- A zone that has been cooling that long **while at least one other zone is waiting** is wound down with the normal overlap handoff, and the waiting zone starts immediately.
- The rotated-out zone goes to the back of the round-robin queue. Its request is still on, so it re-enters the rotation and gets another turn when capacity frees up.
- A zone that just finished a run must **rest** — stay off for as long as its last run lasted (capped at the max run time) — before it can *force* another zone out. This keeps a rotated-out zone from instantly evicting the other long-runner the moment its own wind-down ends. A resting zone still takes naturally-freed capacity immediately, and the Active zones sensor lists `resting` zones so you can see it working.
- If **no** zone is waiting, nothing happens — the zone keeps cooling as long as it needs. The limit only enforces fairness; it never wastes free capacity.

Set it to `0` (the default) to disable the limit entirely.

---

## Installation

### Option A — Direct copy (fastest, no GitHub account needed)

1. Download and extract this repository's ZIP file on your computer.
2. Copy the folder `custom_components/cooling_zone_manager` into your Home Assistant `config/custom_components/` folder (create `custom_components` if it doesn't exist). The easiest ways to reach the config folder are the **Samba share** add-on (browse to it like a network drive) or the **File editor** / **Studio Code Server** add-on.
3. Restart Home Assistant (Settings → System → Restart).
4. Jump to **Configuration** below.

### Option B — Install through HACS

HACS installs integrations from GitHub repositories, so this repo needs to live in a GitHub account you control. No coding involved — everything happens in the web browser.

**Step 1 — Create a free GitHub account** (skip if you have one)

1. Go to <https://github.com> and click **Sign up**. It only needs an email address.

**Step 2 — Create the repository**

1. Once signed in, click the **+** button in the top-right corner → **New repository**.
2. Repository name: `cooling-zone-manager`
3. Description: `Round-robin cooling zone arbiter for Home Assistant` — **don't skip this; HACS requires the repository to have a description.**
4. Leave it set to **Public**, leave every checkbox (README, .gitignore, license) **unchecked**, and click **Create repository**.

**Step 3 — Upload the files**

1. On the empty repository page GitHub shows you, click the link that says **"uploading an existing file."**
2. On your computer, open the extracted ZIP so you can see three items: the `custom_components` folder, `hacs.json`, and `README.md`.
3. Drag all three items together into the upload area in your browser (drag the `custom_components` **folder itself** — the folder structure is kept automatically).
4. Wait for the file list to finish loading, then click the green **Commit changes** button at the bottom.

**Step 4 — Add it to HACS**

1. In Home Assistant, open **HACS** in the sidebar.
2. Click the **⋮ (three-dot) menu** in the top-right → **Custom repositories**.
3. Repository: paste your repo's address: `https://github.com/photomatix18/cooling-zone-manager`
4. Type: **Integration** → click **Add**, then close the dialog.
5. In HACS, search for **Cooling Zone Manager**, open it, and click **Download**.
6. Restart Home Assistant when prompted.

---

## Configuration

1. Go to **Settings → Devices & Services → + Add Integration** and search for **Cooling Zone Manager**.
2. First screen: pick a name, the max zones (e.g. `2`), overlap seconds (e.g. `15`), max zone run time in minutes (e.g. `60` for 1 hour, or `0` for no limit), and optionally an outdoor temperature sensor for temperature-aware capacity.
3. Then add each zone one at a time. Example for a three-zone setup:

   | Zone name | Request entity | Cooling switch |
   |---|---|---|
   | bedrooms | `switch.cooling_request_bedrooms` | `switch.cooling_bedrooms` |
   | kitchen_office | `switch.cooling_request_kitchen_office` | `switch.cooling_kitchen_office` |
   | living_room | `switch.cooling_request_living_room` | `switch.cooling_living_room` |

   Tick **"Add another zone"** before submitting each zone except the last one.
4. Done — the manager starts working immediately.

> **Important:** disable or delete any old automations that were controlling the same cooling switches, otherwise they will fight with the integration.

## Watching it work

The status sensor's attributes show live state. For a play-by-play in the log, add this to `configuration.yaml` and restart:

```yaml
logger:
  logs:
    custom_components.cooling_zone_manager: info
```

Every start, wind-down, re-admit, and shutoff is then logged under Settings → System → Logs.

## Changing things later

- **Max zones / overlap / max run time / temperature thresholds:** change the number entities any time (dashboard, automations, voice — anything that can set a number).
- **Add or remove zones, or change the outdoor temperature sensor:** Settings → Devices & Services → Cooling Zone Manager → **Configure**. Changes apply immediately; removed zones are switched off first if they're cooling. (Renaming a zone = remove it, then add it with the new name.)
- **Updating the code:** if the files change, upload the new versions to the GitHub repo the same drag-and-drop way (GitHub → **Add file → Upload files**) and raise the `"version"` number in `custom_components/cooling_zone_manager/manifest.json` (e.g. `1.1.0` → `1.1.1`). HACS will then offer the update, and after restarting you can confirm the new version on the integration's device page. What changed in each release is listed in [CHANGELOG.md](CHANGELOG.md).

## How the handoff works (details)

When a zone's request drops, the manager starts an internal wind-down timer for it and immediately stops counting it against capacity — so the next waiting zone turns on right away and both run together during the overlap. When the timer fires, the old zone is switched off *only if its request is still off*. Total concurrency is capped at max zones + 1 during a handoff. If Home Assistant restarts mid-overlap, the zone simply gets a fresh wind-down after startup and shuts off cleanly.
