"""Binary sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MowSenseConfigEntry, logic
from .entity import MowSenseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: MowSenseConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    add([Charging(c, "charging"), Problem(c, "problem"), Connected(c, "connected"), InRange(c, "in_range"),
         BladesDue(c, "blades_due")])


class Charging(MowSenseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    @property
    def is_on(self) -> bool | None:
        c = self.coordinator
        return None if c.data is None or c.offline_reason() else c.data.charging


class Problem(MowSenseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool | None:
        d = self.coordinator.data
        if d is None:
            return None
        return bool(d.error_code) or logic.lawn_mower_activity(d.state, d.activity) == "error"


class Connected(MowSenseEntity, BinarySensorEntity):
    """Last poll reached the mower."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self.coordinator.connected

    @property
    def extra_state_attributes(self):
        return {"last_problem": self.coordinator.last_problem}


class InRange(MowSenseEntity, BinarySensorEntity):
    """Mower is advertising (switched on and reachable by a proxy)."""

    _attr_device_class = BinarySensorDeviceClass.PRESENCE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self.coordinator.in_range


class BladesDue(MowSenseEntity, BinarySensorEntity):
    """On once the blades ran their interval since the last change."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.blade_due()
