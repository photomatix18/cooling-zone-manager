"""Config flow for Cooling Zone Manager."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector

from .const import (
    CONF_ADD_ANOTHER,
    CONF_MAX_RUN,
    CONF_MAX_ZONES,
    CONF_OVERLAP,
    CONF_REQUEST_ENTITY,
    CONF_SWITCH_ENTITY,
    CONF_ZONE_NAME,
    CONF_ZONES,
    DEFAULT_MAX_RUN,
    DEFAULT_MAX_ZONES,
    DEFAULT_NAME,
    DEFAULT_OVERLAP,
    DOMAIN,
)

REQUEST_DOMAINS = ["switch", "input_boolean", "binary_sensor", "light"]
SWITCH_DOMAINS = ["switch", "input_boolean", "fan", "light"]


class CoolingZoneManagerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the UI configuration."""

    VERSION = 1

    def __init__(self) -> None:
        self._settings: dict[str, Any] = {}
        self._zones: list[dict[str, str]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """First step: global settings."""
        if user_input is not None:
            self._settings = user_input
            return await self.async_step_zone()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(
                    CONF_MAX_ZONES, default=DEFAULT_MAX_ZONES
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=20, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_OVERLAP, default=DEFAULT_OVERLAP
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=3600,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_MAX_RUN, default=DEFAULT_MAX_RUN
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=28800,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Repeated step: add one zone at a time."""
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_ZONE_NAME].strip()
            request = user_input[CONF_REQUEST_ENTITY]
            switch = user_input[CONF_SWITCH_ENTITY]

            if not name:
                errors[CONF_ZONE_NAME] = "empty_name"
            elif any(zone[CONF_ZONE_NAME] == name for zone in self._zones):
                errors[CONF_ZONE_NAME] = "duplicate_name"
            elif request == switch:
                errors[CONF_SWITCH_ENTITY] = "same_entity"
            elif any(zone[CONF_SWITCH_ENTITY] == switch for zone in self._zones):
                errors[CONF_SWITCH_ENTITY] = "duplicate_switch"

            if not errors:
                self._zones.append(
                    {
                        CONF_ZONE_NAME: name,
                        CONF_REQUEST_ENTITY: request,
                        CONF_SWITCH_ENTITY: switch,
                    }
                )
                if user_input.get(CONF_ADD_ANOTHER):
                    return await self.async_step_zone()
                return self.async_create_entry(
                    title=self._settings.get(CONF_NAME, DEFAULT_NAME),
                    data={
                        CONF_ZONES: self._zones,
                        CONF_MAX_ZONES: int(self._settings[CONF_MAX_ZONES]),
                        CONF_OVERLAP: int(self._settings[CONF_OVERLAP]),
                        CONF_MAX_RUN: int(
                            self._settings.get(CONF_MAX_RUN, DEFAULT_MAX_RUN)
                        ),
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ZONE_NAME): str,
                vol.Required(CONF_REQUEST_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=REQUEST_DOMAINS)
                ),
                vol.Required(CONF_SWITCH_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=SWITCH_DOMAINS)
                ),
                vol.Optional(CONF_ADD_ANOTHER, default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="zone",
            data_schema=schema,
            errors=errors,
            description_placeholders={"zone_count": str(len(self._zones))},
        )
