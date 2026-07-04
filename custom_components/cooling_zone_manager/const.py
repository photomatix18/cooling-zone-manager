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

DEFAULT_NAME = "Cooling Zone Manager"
DEFAULT_MAX_ZONES = 2
DEFAULT_OVERLAP = 15
# 0 disables the max-run rotation.
DEFAULT_MAX_RUN_MINUTES = 0

STORAGE_VERSION = 1
