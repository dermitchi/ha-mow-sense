"""Config flow: find the mower, pair, test once."""

from __future__ import annotations

from collections.abc import Mapping
import random
from typing import Any

from automower_ble.models import MowerModels
import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS, CONF_PIN
from homeassistant.core import callback
from homeassistant.helpers import selector

from .client import AuthError, LinkError, MowerLink, NotInRange, ask
from .const import (
    CONF_ACTIVE_INTERVAL,
    CONF_CHANNEL_ID,
    CONF_GRACE_MINUTES,
    CONF_IDLE_INTERVAL,
    CONF_MIN_RUN,
    CONF_MIN_TEMP,
    CONF_RAIN_DRY,
    CONF_RAIN_LEAD,
    CONF_SCHEDULE_ENTITY,
    CONF_WEATHER_ENTITY,
    DEFAULT_ACTIVE_INTERVAL,
    DEFAULT_MIN_RUN,
    DEFAULT_MIN_TEMP,
    DEFAULT_RAIN_DRY,
    DEFAULT_RAIN_LEAD,
    DEFAULT_GRACE_MINUTES,
    DEFAULT_IDLE_INTERVAL,
    DOMAIN,
    LOGGER,
    SERVICE_UUID,
)

PIN_SCHEMA = vol.Schema({vol.Optional(CONF_PIN): str})


def _pin(value: str | None) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    if not value.isdigit() or len(value) > 4:
        raise ValueError
    return int(value)


class MowSenseConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return MowSenseOptionsFlow()

    async def _test(self, address: str, pin: int | None, channel_id: int) -> tuple[str | None, str]:
        """Connect once. Returns (error, title)."""
        link = MowerLink(self.hass, address, channel_id, pin)

        async def work(m):
            raw = await ask(m, "GetModel")
            name = await ask(m, "GetUserMowerNameAsAsciiString")
            model = None
            if raw:
                info = MowerModels.get((raw["deviceType"], raw["deviceVariant"]))
                model = info.model if info else None
            return (name or "").strip() or model or "Mäher"

        try:
            return None, await link.run(work)
        except AuthError:
            return "invalid_auth", ""
        except NotInRange:
            return "not_in_range", ""
        except LinkError as err:
            LOGGER.warning("Testverbindung zu %s fehlgeschlagen: %s", address, err)
            return "cannot_connect", ""

    async def _finish(self, user_input: dict[str, Any], step_id: str, schema: vol.Schema) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        try:
            pin = _pin(user_input.get(CONF_PIN))
        except ValueError:
            errors[CONF_PIN] = "invalid_pin"
        else:
            channel_id = random.randint(1, 0xFFFFFFFF)
            error, title = await self._test(self._address, pin, channel_id)
            if error is None:
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_ADDRESS: self._address,
                        CONF_CHANNEL_ID: channel_id,
                        CONF_PIN: "" if pin is None else str(pin),
                    },
                )
            errors["base"] = error
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders={"name": self._name or self._address or ""},
        )

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self._name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._finish(user_input, "bluetooth_confirm", PIN_SCHEMA)
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=PIN_SCHEMA,
            description_placeholders={"name": self._name or ""},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        found = {
            info.address: f"{info.name or 'Mäher'} ({info.address})"
            for info in bluetooth.async_discovered_service_info(self.hass, connectable=True)
            if SERVICE_UUID in info.service_uuids
        }
        configured = self._async_current_ids()
        options = [
            selector.SelectOptionDict(value=a, label=label)
            for a, label in found.items()
            if a not in configured
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, custom_value=True)
                ),
                vol.Optional(CONF_PIN): str,
            }
        )
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS].strip().upper()
            await self.async_set_unique_id(self._address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self._finish(user_input, "user", schema)
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                pin = _pin(user_input.get(CONF_PIN))
            except ValueError:
                errors[CONF_PIN] = "invalid_pin"
            else:
                error, _ = await self._test(entry.data[CONF_ADDRESS], pin, entry.data[CONF_CHANNEL_ID])
                if error is None:
                    return self.async_update_reload_and_abort(
                        entry, data=entry.data | {CONF_PIN: "" if pin is None else str(pin)}
                    )
                errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=PIN_SCHEMA,
            errors=errors,
            description_placeholders={"name": entry.title},
        )


class MowSenseOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(CONF_SCHEDULE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="schedule")
                ),
                vol.Optional(CONF_WEATHER_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")
                ),
                **{
                    vol.Required(key, default=opts.get(key, default)): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=lo, max=hi, step=5, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX)
                    )
                    for key, default, lo, hi in (
                        (CONF_RAIN_LEAD, DEFAULT_RAIN_LEAD, 0, 120),
                        (CONF_RAIN_DRY, DEFAULT_RAIN_DRY, 0, 360),
                        (CONF_MIN_RUN, DEFAULT_MIN_RUN, 10, 240),
                    )
                },
                vol.Required(CONF_MIN_TEMP, default=opts.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=-20, max=15, step=1, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_ACTIVE_INTERVAL, default=opts.get(CONF_ACTIVE_INTERVAL, DEFAULT_ACTIVE_INTERVAL)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=30, max=600, step=10, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_IDLE_INTERVAL, default=opts.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=60, max=3600, step=30, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_GRACE_MINUTES, default=opts.get(CONF_GRACE_MINUTES, DEFAULT_GRACE_MINUTES)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=720, step=5, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX)
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {CONF_SCHEDULE_ENTITY: opts.get(CONF_SCHEDULE_ENTITY), CONF_WEATHER_ENTITY: opts.get(CONF_WEATHER_ENTITY)},
            ),
        )
