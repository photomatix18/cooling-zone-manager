"""Config flow for Cooling Zone Manager."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ADD_ANOTHER,
    CONF_MAX_RUN_MINUTES,
    CONF_MAX_ZONES,
    CONF_OVERLAP,
    CONF_REQUEST_ENTITY,
    CONF_SWITCH_ENTITY,
    CONF_TEMP_SENSOR,
    CONF_ZONE_NAME,
    CONF_ZONES,
    DEFAULT_MAX_RUN_MINUTES,
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

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Reconfigure an existing entry without re-adding it."""
        return CoolingZoneOptionsFlow()

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
                    CONF_MAX_RUN_MINUTES, default=DEFAULT_MAX_RUN_MINUTES
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=480,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                vol.Optional(CONF_TEMP_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="temperature"
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
            errors = _validate_zone(self._zones, name, request, switch)

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
                data = {
                    CONF_ZONES: self._zones,
                    CONF_MAX_ZONES: int(self._settings[CONF_MAX_ZONES]),
                    CONF_OVERLAP: int(self._settings[CONF_OVERLAP]),
                    CONF_MAX_RUN_MINUTES: int(
                        self._settings.get(
                            CONF_MAX_RUN_MINUTES, DEFAULT_MAX_RUN_MINUTES
                        )
                    ),
                }
                if self._settings.get(CONF_TEMP_SENSOR):
                    data[CONF_TEMP_SENSOR] = self._settings[CONF_TEMP_SENSOR]
                return self.async_create_entry(
                    title=self._settings.get(CONF_NAME, DEFAULT_NAME),
                    data=data,
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


def _validate_zone(
    zones: list[dict[str, str]], name: str, request: str, switch: str
) -> dict[str, str]:
    """Shared zone validation for the config and options flows."""
    errors: dict[str, str] = {}
    if not name:
        errors[CONF_ZONE_NAME] = "empty_name"
    elif any(zone[CONF_ZONE_NAME] == name for zone in zones):
        errors[CONF_ZONE_NAME] = "duplicate_name"
    elif request == switch:
        errors[CONF_SWITCH_ENTITY] = "same_entity"
    elif any(zone[CONF_SWITCH_ENTITY] == switch for zone in zones):
        errors[CONF_SWITCH_ENTITY] = "duplicate_switch"
    return errors


class CoolingZoneOptionsFlow(config_entries.OptionsFlow):
    """Change the temperature sensor or the zone list on a live entry.

    Every action updates the entry's data; the update listener in
    ``__init__.py`` then reloads the integration to apply it.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Menu of the things that can be changed."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["temp_sensor", "add_zone", "remove_zone"],
        )

    def _update_data(self, new_data: dict[str, Any]) -> None:
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=new_data
        )

    async def async_step_temp_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick, change, or clear the outdoor temperature sensor."""
        if user_input is not None:
            new_data = dict(self.config_entry.data)
            if user_input.get(CONF_TEMP_SENSOR):
                new_data[CONF_TEMP_SENSOR] = user_input[CONF_TEMP_SENSOR]
            else:
                new_data.pop(CONF_TEMP_SENSOR, None)
            self._update_data(new_data)
            return self.async_create_entry(title="", data={})

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_TEMP_SENSOR,
                    description={
                        "suggested_value": self.config_entry.data.get(
                            CONF_TEMP_SENSOR
                        )
                    },
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="temperature"
                    )
                ),
            }
        )
        return self.async_show_form(step_id="temp_sensor", data_schema=schema)

    async def async_step_add_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Add one zone to the live entry."""
        errors: dict[str, str] = {}
        zones = [dict(zone) for zone in self.config_entry.data[CONF_ZONES]]

        if user_input is not None:
            name = user_input[CONF_ZONE_NAME].strip()
            request = user_input[CONF_REQUEST_ENTITY]
            switch = user_input[CONF_SWITCH_ENTITY]
            errors = _validate_zone(zones, name, request, switch)
            if not errors:
                zones.append(
                    {
                        CONF_ZONE_NAME: name,
                        CONF_REQUEST_ENTITY: request,
                        CONF_SWITCH_ENTITY: switch,
                    }
                )
                self._update_data({**self.config_entry.data, CONF_ZONES: zones})
                if user_input.get(CONF_ADD_ANOTHER):
                    return await self.async_step_add_zone()
                return self.async_create_entry(title="", data={})

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
            step_id="add_zone",
            data_schema=schema,
            errors=errors,
            description_placeholders={"zone_count": str(len(zones))},
        )

    async def async_step_remove_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Remove one or more zones from the live entry."""
        errors: dict[str, str] = {}
        zones = [dict(zone) for zone in self.config_entry.data[CONF_ZONES]]

        if user_input is not None:
            remove = set(user_input.get("zones", []))
            if not remove:
                errors["zones"] = "no_zones_selected"
            elif len(remove) >= len(zones):
                errors["zones"] = "keep_one_zone"
            if not errors:
                # Nothing will manage a removed zone anymore, so switch it
                # off if it is still cooling.
                for zone in zones:
                    if zone[CONF_ZONE_NAME] in remove:
                        state = self.hass.states.get(zone[CONF_SWITCH_ENTITY])
                        if state is not None and state.state == "on":
                            await self.hass.services.async_call(
                                "homeassistant",
                                "turn_off",
                                {"entity_id": zone[CONF_SWITCH_ENTITY]},
                                blocking=True,
                            )
                remaining = [
                    zone for zone in zones if zone[CONF_ZONE_NAME] not in remove
                ]
                self._update_data(
                    {**self.config_entry.data, CONF_ZONES: remaining}
                )
                return self.async_create_entry(title="", data={})

        schema = vol.Schema(
            {
                vol.Required("zones", default=[]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[zone[CONF_ZONE_NAME] for zone in zones],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="remove_zone", data_schema=schema, errors=errors
        )
