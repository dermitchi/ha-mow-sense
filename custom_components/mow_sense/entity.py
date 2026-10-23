"""Base entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MowSenseCoordinator


class MowSenseEntity(CoordinatorEntity[MowSenseCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: MowSenseCoordinator, key: str) -> None:
        super().__init__(coordinator)
        address = coordinator.link.address
        self._attr_unique_id = f"{address}_{key}"
        self._attr_translation_key = key
        data = coordinator.data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer="Gardena",
            model=(data.model if data else None) or coordinator.config_entry.title,
            name=coordinator.config_entry.title,
            serial_number=str(data.serial) if data and data.serial else None,
        )

    @property
    def available(self) -> bool:
        # Never "unavailable": offline shows as a reason (Zustand, Tätigkeit,
        # Erreichbarkeit) or as the last known value.
        return True
