"""The mower itself."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MowSenseConfigEntry, logic
from .entity import MowSenseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: MowSenseConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    add([MowSenseMower(entry.runtime_data, "mower")])
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "mow",
        {vol.Optional("hours"): vol.All(vol.Coerce(float), vol.Range(min=0.25, max=18))},
        "async_mow",
    )
    platform.async_register_entity_service(
        "park",
        {vol.Optional("until", default="further_notice"): vol.In(["next_schedule", "further_notice"])},
        "async_park",
    )
    platform.async_register_entity_service("sync_schedule", {}, "async_sync_schedule")


class MowSenseMower(MowSenseEntity, LawnMowerEntity):
    _attr_name = None
    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
    )

    @property
    def activity(self) -> LawnMowerActivity | None:
        d = self.coordinator.data
        value = logic.lawn_mower_activity(d.state, d.activity) if d else None
        return LawnMowerActivity(value) if value else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        return {
            "connected": self.coordinator.connected,
            "automatic": self.coordinator.prefs["automatic"],
            "mode": logic.MODES.get(d.mode) if d else None,
        }

    async def async_start_mowing(self) -> None:
        await self.coordinator.start_mowing()

    async def async_pause(self) -> None:
        await self.coordinator.pause()

    async def async_dock(self) -> None:
        await self.coordinator.park()

    async def async_mow(self, hours: float | None = None) -> None:
        await self.coordinator.start_mowing(hours)

    async def async_park(self, until: str = "further_notice") -> None:
        await self.coordinator.park(until)

    async def async_sync_schedule(self) -> None:
        await self.coordinator.resync_schedule()
