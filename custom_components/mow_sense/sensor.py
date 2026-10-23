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

OFFLINE = ["mower_off", "proxy_offline", "no_answer"]


def _hours(key: str) -> Callable[[MowerData], Any]:
    return lambda d: round(d.stats[key] / 3600, 1) if key in d.stats else None


@dataclass(frozen=True, kw_only=True)
class MowSensor(SensorEntityDescription):
    value: Callable[[MowerData], Any]
    # While offline: "keep" last value, "reason" why offline, "none" = unknown
    offline: str = "keep"


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
        options=list(logic.STATES.values()) + OFFLINE,
        value=lambda d: logic.STATES.get(d.state),
        offline="reason",
    ),
    MowSensor(
        key="activity",
        device_class=SensorDeviceClass.ENUM,
        options=list(logic.ACTIVITIES.values()) + OFFLINE,
        value=lambda d: logic.ACTIVITIES.get(d.activity),
        offline="reason",
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
        offline="none",
    ),
    MowSensor(
        key="cutting_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=_hours("totalCuttingTime"),
    ),
    MowSensor(
        key="running_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=_hours("totalRunningTime"),
    ),
    MowSensor(
        key="blade_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=_hours("cuttingBladeUsageTime"),
    ),
    MowSensor(
        key="collisions",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.stats.get("numberOfCollisions"),
    ),
    MowSensor(
        key="charging_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.stats.get("numberOfChargingCycles"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: MowSenseConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    add([Sensor(c, desc) for desc in SENSORS]
        + [Schedule(c, "schedule"), LastContact(c, "last_contact"), BladeHours(c, "blade_hours"), BladeChanged(c, "blade_changed_at"),
           RainGuard(c, "rain_guard"), Reachability(c, "reachability")])


class Sensor(MowSenseEntity, SensorEntity):
    entity_description: MowSensor

    def __init__(self, coordinator: MowSenseCoordinator, desc: MowSensor) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def native_value(self):
        desc = self.entity_description
        d = self.coordinator.data
        reason = self.coordinator.offline_reason() if desc.offline != "keep" else None
        if desc.offline == "reason" and (reason or d is None):
            return reason or self.coordinator.reachability()
        if d is None or (reason and desc.offline == "none"):
            return None
        return desc.value(d)

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data
        if d is None:
            return None
        attrs = {}
        if not self.coordinator.fresh():
            attrs["values_from"] = d.fetched_at
        if self.entity_description.key == "error" and d.last_message:
            attrs["last_message"] = d.last_message["code"]
            attrs["last_message_time"] = d.last_message["time"]
        return attrs or None


class Schedule(MowSenseEntity, SensorEntity):
    """The schedule stored in the mower, plus whether it matches HA."""

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        tasks = self.coordinator.data.tasks
        return None if tasks is None else logic.schedule_summary(tasks)

    @property
    def extra_state_attributes(self):
        c = self.coordinator
        tasks = (c.data.tasks if c.data else None) or []
        return {
            "sync": c.schedule_status,
            "tasks": [t.label() for t in tasks],
            "last_correction": c.last_correction,
        }


class LastContact(MowSenseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return self.coordinator.last_contact


class BladeHours(MowSenseEntity, SensorEntity):
    """Cutting hours since the last blade change (counted in HA)."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_suggested_display_precision = 1

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
    def native_value(self):
        value = self.coordinator.prefs["blade_changed"]
        return dt_util.parse_datetime(value) if value else None


class RainGuard(MowSenseEntity, SensorEntity):
    """off | clear | rain_soon | raining | drying"""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["off", "clear", "rain_soon", "raining", "drying", "frost"]

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


class Reachability(MowSenseEntity, SensorEntity):
    """online | no_answer | mower_off | proxy_offline"""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["online", *OFFLINE]

    @property
    def native_value(self) -> str:
        return self.coordinator.reachability()

    @property
    def extra_state_attributes(self):
        c = self.coordinator
        return {"proxy": c.proxy_name(), "last_problem": c.last_problem}
