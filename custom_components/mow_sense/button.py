"""Buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MowSenseConfigEntry
from .entity import MowSenseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: MowSenseConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    add([Refresh(c, "refresh"), ParkFurtherNotice(c, "park_further_notice"), BladesChanged(c, "blades_changed")])


class Refresh(MowSenseEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self.coordinator.resync_schedule()


class ParkFurtherNotice(MowSenseEntity, ButtonEntity):
    # Pressing it while offline gives a clear error instead of a greyed-out button.

    async def async_press(self) -> None:
        await self.coordinator.park("further_notice")


class BladesChanged(MowSenseEntity, ButtonEntity):
    """Press after fitting new blades; the counter in HA starts again."""

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self.coordinator.blades_changed()
