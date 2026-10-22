"""Mow Sense – Gardena / Husqvarna mowers over Bluetooth, with HA in charge."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_PIN, Platform
from homeassistant.core import HomeAssistant

from .client import MowerLink
from .const import CONF_CHANNEL_ID
from .coordinator import MowSenseCoordinator

PLATFORMS = [
    Platform.LAWN_MOWER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BUTTON,
]

type MowSenseConfigEntry = ConfigEntry[MowSenseCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MowSenseConfigEntry) -> bool:
    pin = entry.data.get(CONF_PIN)
    link = MowerLink(
        hass,
        entry.data[CONF_ADDRESS],
        entry.data[CONF_CHANNEL_ID],
        int(pin) if pin not in (None, "") else None,
    )
    coordinator = MowSenseCoordinator(hass, entry, link)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    # Never block startup on the mower: it may be switched off for the winter.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"mow_sense first poll {entry.title}"
    )
    return True


async def _options_updated(hass: HomeAssistant, entry: MowSenseConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MowSenseConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
