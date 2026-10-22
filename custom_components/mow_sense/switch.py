"""Automatik switch: may the mower drive on its own schedule?"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MowSenseConfigEntry
from .entity import MowSenseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: MowSenseConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    add([Automatic(entry.runtime_data, "automatic")])


class Automatic(MowSenseEntity, SwitchEntity):
    """On: mower follows its schedule. Off: it only drives when HA starts it."""

    @property
    def available(self) -> bool:
        return True  # remembered in HA, applied on next contact

    @property
    def is_on(self) -> bool:
        return self.coordinator.prefs["automatic"]

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.set_automatic(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.set_automatic(False)
        self.async_write_ha_state()
