# Cooling Zone Manager

A custom Home Assistant integration that lets **at most N cooling zones run at the same time**, with round-robin fairness and an overlap window when one zone hands off to the next. Everything is configured in the Home Assistant UI — no YAML, no automations, no helpers to create.

## What it does

- A zone starts cooling as soon as its **request entity** turns on, if capacity allows.
- Fairness is **round-robin**: when there are more requests than slots, the zone that has waited longest gets the next free slot.
- When a zone's request turns off, the next waiting zone starts **immediately**, and the old zone keeps running for the **overlap time** before shutting off (a smooth handoff).
- If the old zone's request comes back during the overlap, it just keeps running — no short-cycling.
- Lowering **Max zones** trims the oldest-running zones immediately.
- The manager re-derives everything from live entity states, so it self-heals after Home Assistant restarts, reloads, or manual switch flips. (A zone switched on by hand with no request will be wound down after the overlap time.)

## Entities it creates

One device ("Cooling Zone Manager") with three entities:

| Entity | Purpose |
|---|---|
| `number.cooling_zone_manager_max_zones` | Max zones allowed at once — change it any time |
| `number.cooling_zone_manager_overlap_time` | Overlap / wind-down time in seconds |
| `sensor.cooling_zone_manager_active_zones` | How many zones are cooling now; attributes show active / requesting / winding-down / waiting zones and the round-robin order |

Both numbers remember their values across restarts, so they replace the old `input_number` helpers.

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
3. Repository: paste your repo's address, e.g. `https://github.com/YOUR-USERNAME/cooling-zone-manager`
4. Type: **Integration** → click **Add**, then close the dialog.
5. In HACS, search for **Cooling Zone Manager**, open it, and click **Download**.
6. Restart Home Assistant when prompted.

---

## Configuration

1. Go to **Settings → Devices & Services → + Add Integration** and search for **Cooling Zone Manager**.
2. First screen: pick a name, the max zones (e.g. `2`) and overlap seconds (e.g. `15`).
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

- **Max zones / overlap:** change the two number entities any time (dashboard, automations, voice — anything that can set a number).
- **Add, remove, or rename zones:** delete the integration entry (Settings → Devices & Services → Cooling Zone Manager → ⋮ → Delete) and add it again with the new zone list. Takes about a minute.
- **Updating the code:** if the files change, upload the new versions to the GitHub repo the same drag-and-drop way (GitHub → **Add file → Upload files**) and raise the `"version"` number in `custom_components/cooling_zone_manager/manifest.json` (e.g. `1.0.0` → `1.0.1`). HACS will then offer the update.

## How the handoff works (details)

When a zone's request drops, the manager starts an internal wind-down timer for it and immediately stops counting it against capacity — so the next waiting zone turns on right away and both run together during the overlap. When the timer fires, the old zone is switched off *only if its request is still off*. Total concurrency is capped at max zones + 1 during a handoff. If Home Assistant restarts mid-overlap, the zone simply gets a fresh wind-down after startup and shuts off cleanly.
