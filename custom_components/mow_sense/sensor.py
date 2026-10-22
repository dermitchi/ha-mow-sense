"""Sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import MowSenseConfigEntry, logic
from .coordinator import ERROR_NAMES, MowerData, MowSenseCoordinator
from .entity import MowSenseEntity


def _hours(key: str) -> Callable[[MowerData], Any]:
    return lambda d: round(d.stats[key] / 3600, 1) if key in d.stats else None


@dataclass(frozen=True, kw_only=True)
class MowSensor(SensorEntityDescription):
    value: Callable[[MowerData], Any]
    fresh: bool = True


SENSORS = (
    MowSensor(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda d: d.battery,
    ),
    MowSensor(
        key="state",
        device_class=SensorDeviceClass.ENUM,
        options=list(logic.STATES.values()),
        value=lambda d: logic.STATES.get(d.state),
    ),
    MowSensor(
        key="activity",
        device_class=SensorDeviceClass.ENUM,
        options=list(logic.ACTIVITIES.values()),
        value=lambda d: logic.ACTIVITIES.get(d.activity),
    ),
    MowSensor(
        key="mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(logic.MODES.values()),
        value=lambda d: logic.MODES.get(d.mode),
    ),
    MowSensor(
        key="error",
        value=lambda d: logic.error_text(d.error_code, ERROR_NAMES),
    ),
    MowSensor(
        key="next_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda d: d.next_start,
    ),
    MowSensor(
        key="remaining_charge",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value=lambda d: d.remaining_charge_min,
    ),
    MowSensor(
        key="cutting_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=_hours("totalCuttingTime"),
        fresh=False,
    ),
    MowSensor(
        key="running_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=_hours("totalRunningTime"),
        fresh=False,
    ),
    MowSensor(
        key="blade_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=_hours("cuttingBladeUsageTime"),
        fresh=False,
    ),
    MowSensor(
        key="collisions",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.stats.get("numberOfCollisions"),
        fresh=False,
    ),
    MowSensor(
        key="charging_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.stats.get("numberOfChargingCycles"),
        fresh=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: MowSenseConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    add([Sensor(c, desc) for desc in SENSORS]
        + [Schedule(c, "schedule"), LastContact(c, "last_contact"), BladeHours(c, "blade_hours"), BladeChanged(c, "blade_changed_at"),
           RainGuard(c, "rain_guard")])


class Sensor(MowSenseEntity, SensorEntity):
    entity_description: MowSensor

    def __init__(self, coordinator: MowSenseCoordinator, desc: MowSensor) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc
        self._needs_fresh = desc.fresh

    @property
    def native_value(self):
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data
        if self.entity_description.key == "error" and d.last_message:
            return {
                "last_message": d.last_message["code"],
                "last_message_time": d.last_message["time"],
            }
        return None


class Schedule(MowSenseEntity, SensorEntity):
    """The schedule stored in the mower, plus whether it matches HA."""

    _needs_fresh = False

    @property
    def native_value(self):
        tasks = self.coordinator.data.tasks
        return None if tasks is None else logic.schedule_summary(tasks)

    @property
    def extra_state_attributes(self):
        c = self.coordinator
        tasks = c.data.tasks or []
        return {
            "sync": c.schedule_status,
            "tasks": [t.label() for t in tasks],
            "last_correction": c.last_correction,
        }


class LastContact(MowSenseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _needs_fresh = False

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return self.coordinator.last_contact


class BladeHours(MowSenseEntity, SensorEntity):
    """Cutting hours since the last blade change (counted in HA)."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_suggested_display_precision = 1
    _needs_fresh = False

    @property
    def available(self) -> bool:
        return self.coordinator.blade_hours() is not None

    @property
    def native_value(self):
        return self.coordinator.blade_hours()

    @property
    def extra_state_attributes(self):
        used = self.coordinator.blade_hours()
        interval = self.coordinator.prefs["blade_interval"]
        return {
            "interval": interval,
            "remaining": None if used is None else round(max(0.0, interval - used), 1),
            "percent": None if used is None else min(100, round(used / interval * 100)),
        }


class BladeChanged(MowSenseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        value = self.coordinator.prefs["blade_changed"]
        return dt_util.parse_datetime(value) if value else None


class RainGuard(MowSenseEntity, SensorEntity):
    """off | clear | rain_soon | raining | drying"""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["off", "clear", "rain_soon", "raining", "drying"]

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return self.coordinator.rain_status()

    @property
    def extra_state_attributes(self):
        r = self.coordinator.rain
        return {
            "next_rain": r.next_rain,
            "rain_end": r.rain_end,
            "dry_from": r.blocked_until,
            "home_by": r.leave_by,
            "waiting_until": (self.coordinator.prefs.get("pending") or {}).get("until"),
        }
