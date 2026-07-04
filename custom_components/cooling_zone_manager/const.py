"""Constants for the Cooling Zone Manager integration."""

DOMAIN = "cooling_zone_manager"

CONF_ZONES = "zones"
CONF_ZONE_NAME = "name"
CONF_REQUEST_ENTITY = "request_entity"
CONF_SWITCH_ENTITY = "switch_entity"
CONF_MAX_ZONES = "max_zones"
CONF_OVERLAP = "overlap_seconds"
CONF_MAX_RUN = "max_run_seconds"
CONF_ADD_ANOTHER = "add_another"

DEFAULT_NAME = "Cooling Zone Manager"
DEFAULT_MAX_ZONES = 2
DEFAULT_OVERLAP = 15
# 0 disables the max-run rotation.
DEFAULT_MAX_RUN = 0

STORAGE_VERSION = 1
