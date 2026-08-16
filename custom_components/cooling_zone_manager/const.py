"""Constants for the Cooling Zone Manager integration."""

DOMAIN = "cooling_zone_manager"

CONF_ZONES = "zones"
CONF_ZONE_NAME = "name"
CONF_REQUEST_ENTITY = "request_entity"
CONF_SWITCH_ENTITY = "switch_entity"
CONF_MAX_ZONES = "max_zones"
CONF_OVERLAP = "overlap_seconds"
CONF_MAX_RUN_MINUTES = "max_run_minutes"
# Legacy key from 1.1.0 entries, which stored seconds.
CONF_MAX_RUN = "max_run_seconds"
CONF_TEMP_SENSOR = "temp_sensor"
CONF_ADD_ANOTHER = "add_another"

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

DEFAULT_NAME = "Cooling Zone Manager"
DEFAULT_MAX_ZONES = 2
DEFAULT_OVERLAP = 15
# 0 disables the max-run rotation.
DEFAULT_MAX_RUN_MINUTES = 0

STORAGE_VERSION = 1
