"""How long 'Start mowing' runs."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MowSenseConfigEntry
from .entity import MowSenseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: MowSenseConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    add([MowHours(c, "mow_hours"), BladeInterval(c, "blade_interval")])


class MowHours(MowSenseEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0.5
    _attr_native_max_value = 12
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_mode = NumberMode.BOX

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float:
        return self.coordinator.prefs["mow_hours"]

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.prefs["mow_hours"] = value
        await self.coordinator.save_prefs()
        self.async_write_ha_state()


class BladeInterval(MowSenseEntity, NumberEntity):
    """Cutting hours after which the blades should be changed."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 20
    _attr_native_max_value = 500
    _attr_native_step = 5
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_mode = NumberMode.BOX

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float:
        return self.coordinator.prefs["blade_interval"]

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.prefs["blade_interval"] = value
        await self.coordinator.save_prefs()
        self.coordinator.async_update_listeners()
