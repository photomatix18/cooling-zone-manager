"""Constants for the HVAC Zone Manager integration."""

DOMAIN = "hvac_zone_manager"

CONF_ZONES = "zones"
CONF_ZONE_NAME = "name"
CONF_REQUEST_ENTITY = "request_entity"
CONF_SWITCH_ENTITY = "switch_entity"
CONF_MAX_ZONES = "max_zones"
CONF_OVERLAP = "overlap_seconds"
CONF_MAX_RUN_MINUTES = "max_run_minutes"
CONF_COOLDOWN_MINUTES = "cooldown_minutes"
# Legacy key from 1.1.0 entries, which stored seconds.
CONF_MAX_RUN = "max_run_seconds"
CONF_TEMP_SENSOR = "temp_sensor"
CONF_ADD_ANOTHER = "add_another"
CONF_MODE = "mode"
CONF_MIN_ZONES = "min_zones"
CONF_COMPANION_MARGIN = "companion_margin"
CONF_CLIMATE_ENTITY = "climate_entity"

# One entry manages one direction. Cooling caps how many zones run at
# once; heating sets a floor, because one zone alone is too little load
# for the furnace. Entries made before 2.1.0 have no mode and are cooling.
MODE_COOLING = "cooling"
MODE_HEATING = "heating"
MODES = [MODE_COOLING, MODE_HEATING]

# Minimum zones running together whenever any zone runs. 0 or 1 = off.
DEFAULT_MIN_ZONES_HEATING = 2
# A companion — a zone run for load, not because its room asked — gives
# its slot back once the room is this far past its own setpoint (in the
# climate entity's unit), and is not picked again until it has fallen
# COMPANION_HYSTERESIS back inside that margin.
DEFAULT_COMPANION_MARGIN_F = 2.0
DEFAULT_COMPANION_MARGIN_C = 1.0
COMPANION_HYSTERESIS = 0.5

# Degrees the outdoor temperature must cross a threshold by before the
# allowed zone count changes, so a reading hovering at a threshold does
# not flap capacity up and down.
TEMP_HYSTERESIS = 1.0

# Zone priorities: 1 runs first, 3 yields to everyone. Automations (or a
# dashboard card) drive the per-zone number entities; the manager only
# obeys the current values. With every zone at the default the scheduler
# behaves exactly as it did before priorities existed.
PRIORITY_MIN = 1
PRIORITY_MAX = 3
DEFAULT_PRIORITY = 2

# A runner may not be evicted by a higher-priority waiter until it has
# cooled this long (seconds) — a compressor guard so boosting a zone
# cannot hard-cut a cycle that just started.
PRIORITY_MIN_RUN = 300

# Outdoor lockout defaults: cooling never runs below the cooling value,
# heating never above the heating value. TEMP_HYSTERESIS applies on the
# way back out, so a reading hovering at the line does not flap.
DEFAULT_COOLING_LOCKOUT_F = 60.0
DEFAULT_COOLING_LOCKOUT_C = 15.5
DEFAULT_HEATING_LOCKOUT_F = 65.0
DEFAULT_HEATING_LOCKOUT_C = 18.5

# Changeover between the cooling and heating entries. After one direction
# has run, the other may not start until the first has been off this long
# (minutes); 0 leaves only the interlock (never both at once).
DEFAULT_CHANGEOVER_MINUTES = 30
# ...and, for a thermostat in auto / heat_cool, not until its room is this
# far past setpoint in the new direction — the latch that keeps an auto
# thermostat from flipping on a fraction of a degree of overshoot.
DEFAULT_CHANGEOVER_BAND_F = 2.0
DEFAULT_CHANGEOVER_BAND_C = 1.0
# How often (seconds) a zone held by the changeover band re-checks its
# room. The room temperature lives on a thermostat this entry may not
# subscribe to, so it polls while held.
CHANGEOVER_BAND_RECHECK = 60

# How long (seconds) after startup to wait for the outdoor sensor's
# first reading. Until it arrives (or this passes), capacity changes are
# frozen: running zones keep running, no extra zones start, nothing is
# shed. Without this, a restart on a hot day briefly sees "no sensor"
# -> full manual capacity, starts every requesting zone, then sheds the
# extras seconds later when the first reading lands.
TEMP_STARTUP_GRACE = 300

# How long (seconds) the manager's own unconfirmed turn-on/off command
# overrides the switch's observed state. The template switches report
# back through HomeKit 13-23 s late; trusting the observed state in that
# window double-books the slot (a second zone is admitted, then the
# capacity shed switches the first one off ~50 s after it started).
PENDING_GRACE = 60

DEFAULT_NAME = "HVAC Zone Manager"
DEFAULT_MAX_ZONES = 2
DEFAULT_OVERLAP = 15
# 0 disables the max-run rotation.
DEFAULT_MAX_RUN_MINUTES = 0
# Minimum compressor off time: once every zone is off, nothing may start
# again until this much time has passed since the last zone switched off.
# 0 disables it. A heating entry has its own knob, so the two can differ
# or be off independently.
DEFAULT_COOLDOWN_MINUTES = 5

STORAGE_VERSION = 1
