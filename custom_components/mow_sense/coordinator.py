"""Polling, last-known values and the 'HA is in charge' rules."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import time
from typing import Any

from automower_ble.error_codes import ErrorCodes
from automower_ble.models import MowerModels
from automower_ble.mower import Mower

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import logic
from .client import AuthError, LinkError, MowerLink, NotInRange, ask
from .const import (
    CLOCK_TOLERANCE,
    CONF_ACTIVE_INTERVAL,
    CONF_GRACE_MINUTES,
    CONF_IDLE_INTERVAL,
    CONF_SCHEDULE_ENTITY,
    DEFAULT_ACTIVE_INTERVAL,
    DEFAULT_GRACE_MINUTES,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_MOW_HOURS,
    DOMAIN,
    EVENT_CORRECTED,
    LOGGER,
    MAX_SCHEDULE_ATTEMPTS,
    RETRY_INTERVAL,
    SLOW_EVERY,
    TASKS_EVERY,
)

ERROR_NAMES = {e.value: e.name for e in ErrorCodes}


@dataclass
class MowerData:
    fetched_at: datetime
    battery: int | None = None
    charging: bool | None = None
    remaining_charge_min: int | None = None
    state: int | None = None
    activity: int | None = None
    mode: int | None = None
    error_code: int | None = None
    override: logic.Override = field(default_factory=logic.Override)
    next_start: datetime | None = None
    tasks: list[logic.Task] | None = None
    stats: dict[str, int] = field(default_factory=dict)
    last_message: dict[str, Any] | None = None
    clock_drift: int | None = None
    model: str | None = None
    serial: int | None = None
    name: str | None = None


class MowSenseCoordinator(DataUpdateCoordinator[MowerData | None]):
    """Owns all mower sessions."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, link: MowerLink) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=RETRY_INTERVAL),
        )
        self.link = link
        self.connected = False
        self.in_range = False
        self.last_contact: datetime | None = None
        self.last_problem: str | None = None
        self.schedule_status = "off"  # off | in_sync | written | failed
        self.last_correction: str | None = None
        self._store: Store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.prefs: dict[str, Any] = {"automatic": True, "mow_hours": DEFAULT_MOW_HOURS}
        self._slow_at = 0.0
        self._tasks_at = 0.0
        self._schedule_attempts = 0
        self._wanted_tasks: list[logic.Task] | None = None
        self._unsub_ble = None

    # --- setup ---------------------------------------------------------------

    async def async_setup(self) -> None:
        if stored := await self._store.async_load():
            self.prefs.update(stored)
        self.in_range = self.link.in_range()
        self._unsub_ble = bluetooth.async_register_callback(
            self.hass,
            self._on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.link.address, connectable=True),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
        self.config_entry.async_on_unload(self._unsub_ble)
        self.config_entry.async_on_unload(
            bluetooth.async_track_unavailable(
                self.hass, self._on_unavailable, self.link.address, connectable=True
            )
        )

    @callback
    def _on_advertisement(self, info, change) -> None:
        if not self.in_range:
            # Back from being switched off / out of range: poll right away.
            self.in_range = True
            LOGGER.debug("Mäher wieder in Reichweite")
            self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_unavailable(self, info) -> None:
        self.in_range = False
        self.connected = False
        self.async_update_listeners()

    async def save_prefs(self) -> None:
        await self._store.async_save(self.prefs)

    # --- options --------------------------------------------------------------

    def _opt(self, key: str, default):
        return self.config_entry.options.get(key, default)

    @property
    def grace(self) -> timedelta:
        return timedelta(minutes=self._opt(CONF_GRACE_MINUTES, DEFAULT_GRACE_MINUTES))

    def fresh(self) -> bool:
        return self.data is not None and dt_util.utcnow() - self.data.fetched_at < self.grace

    def mower_now(self) -> int:
        return logic.to_mower_time(dt_util.now())

    # --- polling --------------------------------------------------------------

    async def _async_update_data(self) -> MowerData | None:
        self.in_range = self.link.in_range()
        try:
            if not self.in_range:
                raise NotInRange("Mäher nicht in Reichweite (aus oder zu weit weg)")
            data = await self.link.run(self._poll)
        except AuthError as err:
            self.connected = False
            raise ConfigEntryAuthFailed(str(err)) from err
        except LinkError as err:
            self.connected = False
            self.last_problem = str(err)
            self._set_interval(None)
            if self.data is None:
                raise UpdateFailed(str(err)) from err
            LOGGER.debug("Behalte letzte Werte: %s", err)
            return self.data
        self.connected = True
        self.last_problem = None
        self.last_contact = data.fetched_at
        self._update_device(data)
        self._set_interval(data)
        return data

    def _update_device(self, data: MowerData) -> None:
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, self.link.address)})
        serial = str(data.serial) if data.serial else None
        if device and (device.model != data.model or device.serial_number != serial):
            registry.async_update_device(device.id, model=data.model, serial_number=serial)

    def _set_interval(self, data: MowerData | None) -> None:
        if data is None:
            seconds = RETRY_INTERVAL
        elif logic.is_active(data.state, data.activity):
            seconds = self._opt(CONF_ACTIVE_INTERVAL, DEFAULT_ACTIVE_INTERVAL)
        else:
            seconds = self._opt(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)
        self.update_interval = timedelta(seconds=seconds)

    async def _poll(self, m: Mower) -> MowerData:
        prev = self.data
        now_mono = time.monotonic()
        tz = dt_util.get_default_time_zone()
        d = MowerData(fetched_at=dt_util.utcnow())
        if prev:
            d = replace(
                prev,
                fetched_at=d.fetched_at,
                remaining_charge_min=None,
            )

        if d.model is None:
            raw = await ask(m, "GetModel")
            if raw:
                info = MowerModels.get((raw["deviceType"], raw["deviceVariant"]))
                d.model = info.model if info else f"Unbekannt ({raw['deviceType']},{raw['deviceVariant']})"
            d.serial = await ask(m, "GetSerialNumber")
            d.name = await ask(m, "GetUserMowerNameAsAsciiString")

        d.battery = await ask(m, "GetBatteryLevel")
        if d.battery is None:
            raise LinkError("Keine Antwort vom Mäher")
        d.charging = bool(await ask(m, "IsCharging"))
        d.state = await ask(m, "GetState")
        d.activity = await ask(m, "GetActivity")
        d.mode = await ask(m, "GetMode")
        d.error_code = await ask(m, "GetError")
        d.override = logic.override_from_raw(await ask(m, "GetOverride"))
        d.next_start = logic.from_mower_time(await ask(m, "GetNextStartTime"), tz)
        if d.charging:
            secs = await ask(m, "GetRemainingChargingTime")
            d.remaining_charge_min = round(secs / 60) if secs is not None else None

        slow = now_mono - self._slow_at >= SLOW_EVERY
        if slow:
            stats = await ask(m, "GetAllStatistics")
            if isinstance(stats, dict):
                d.stats = stats
            mower_clock = await ask(m, "GetTime")
            if mower_clock:
                d.clock_drift = mower_clock - self.mower_now()
                if abs(d.clock_drift) > CLOCK_TOLERANCE:
                    LOGGER.info("Stelle Mäher-Uhr (Abweichung %s s)", d.clock_drift)
                    await ask(m, "SetTime", time=self.mower_now())
                    d.clock_drift = 0

        await self._enforce_mode(m, d)
        await self._enforce_schedule(m, d, now_mono)

        if slow:
            # Asked last: the library notes GetMessage can upset later commands.
            msg = await ask(m, "GetMessage", messageId=0)
            if isinstance(msg, dict) and msg.get("time"):
                d.last_message = {
                    "time": logic.from_mower_time(msg["time"], tz),
                    "code": logic.error_text(msg.get("code"), ERROR_NAMES),
                    "severity": msg.get("severity"),
                }
            self._slow_at = now_mono
        return d

    # --- rules ------------------------------------------------------------------

    async def _enforce_mode(self, m: Mower, d: MowerData) -> None:
        """Automatik off -> the mower must not drive on its own schedule."""
        if self.prefs["automatic"] or d.mode != logic.MODE_AUTO:
            return
        if d.override.action == logic.OVERRIDE_MOW and d.override.active(self.mower_now()):
            return  # a run HA (or someone) started on purpose
        outside = logic.is_outside(d.activity)
        LOGGER.warning(
            "Automatik ist aus, Mäher stand auf AUTO%s – setze 'Parken bis auf Weiteres'",
            " und fuhr" if outside else "",
        )
        await ask(m, "SetMode", mode=logic.MODE_HOME)
        await ask(m, "StartTrigger")  # apply, like the app does
        d.mode = logic.MODE_HOME
        self._correction("mode", "Automatik aus: Mäher auf 'Parken bis auf Weiteres' gestellt", outside)

    async def _wanted_schedule(self) -> list[logic.Task] | None:
        entity_id = self._opt(CONF_SCHEDULE_ENTITY, None)
        if not entity_id:
            return None
        if self.hass.states.get(entity_id) is None:
            raise HomeAssistantError(f"{entity_id} existiert nicht")
        resp = await self.hass.services.async_call(
            "schedule",
            "get_schedule",
            target={"entity_id": entity_id},
            blocking=True,
            return_response=True,
        )
        return logic.tasks_from_schedule(resp[entity_id])

    async def _read_tasks(self, m: Mower) -> list[logic.Task]:
        count = await ask(m, "GetNumberOfTasks") or 0
        tasks = []
        for i in range(count):
            raw = await ask(m, "GetTask", taskId=i)
            if isinstance(raw, dict):
                tasks.append(logic.task_from_raw(raw))
        return tasks

    async def _enforce_schedule(self, m: Mower, d: MowerData, now_mono: float) -> None:
        try:
            wanted = await self._wanted_schedule()
        except HomeAssistantError as err:
            LOGGER.warning("Zeitplan-Helfer nicht lesbar: %s", err)
            wanted = None
        if wanted != self._wanted_tasks:
            self._wanted_tasks = wanted
            self._schedule_attempts = 0
            self._tasks_at = 0.0
        if d.tasks is not None and now_mono - self._tasks_at < TASKS_EVERY:
            return
        d.tasks = await self._read_tasks(m)
        self._tasks_at = now_mono
        if wanted is None:
            self.schedule_status = "off"
            return
        if logic.same_tasks(d.tasks, wanted):
            self.schedule_status = "in_sync"
            self._schedule_attempts = 0
            return
        if self._schedule_attempts >= MAX_SCHEDULE_ATTEMPTS:
            self.schedule_status = "failed"
            return
        self._schedule_attempts += 1
        LOGGER.warning(
            "Zeitplan im Mäher (%s) weicht ab – schreibe HA-Zeitplan (%s)",
            logic.schedule_summary(d.tasks),
            logic.schedule_summary(wanted),
        )
        await ask(m, "StartTaskTransaction")
        await ask(m, "DeleteAllTask")
        for task in wanted:
            await ask(m, "AddTask", **task.kwargs())
        await ask(m, "CommitTaskTransaction")
        d.tasks = await self._read_tasks(m)
        if logic.same_tasks(d.tasks, wanted):
            self.schedule_status = "written"
            self._schedule_attempts = 0
            self._correction("schedule", f"Zeitplan aus HA übertragen: {logic.schedule_summary(wanted)}", False)
        else:
            self.schedule_status = "failed"
            LOGGER.error(
                "Mäher hat den Zeitplan nicht übernommen (gelesen: %s)", logic.schedule_summary(d.tasks)
            )

    def _correction(self, kind: str, text: str, was_driving: bool) -> None:
        self.last_correction = f"{dt_util.now().strftime('%d.%m. %H:%M')} {text}"
        self.hass.bus.async_fire(
            EVENT_CORRECTED,
            {"entry_id": self.config_entry.entry_id, "kind": kind, "text": text, "was_driving": was_driving},
        )

    # --- commands -----------------------------------------------------------------

    async def command(self, work) -> None:
        if not self.link.in_range():
            raise HomeAssistantError("Mäher nicht in Bluetooth-Reichweite (aus oder zu weit weg)")
        try:
            await self.link.run(work)
        except AuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except LinkError as err:
            raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()

    async def start_mowing(self, hours: float | None = None) -> None:
        hours = hours or self.prefs["mow_hours"]

        async def work(m: Mower) -> None:
            state = await ask(m, "GetState")
            activity = await ask(m, "GetActivity")
            if state == logic.STATE_PAUSED and logic.is_outside(activity):
                await ask(m, "StartTrigger")  # resume where it stopped
                return
            await ask(m, "SetMode", mode=logic.MODE_AUTO)
            await ask(m, "SetOverrideMow", duration=int(hours * 3600))
            await ask(m, "StartTrigger")

        await self.command(work)

    async def pause(self) -> None:
        await self.command(lambda m: ask(m, "Pause"))

    async def park(self, until: str | None = None) -> None:
        """until: 'next_schedule' or 'further_notice' (default depends on Automatik)."""
        if until is None:
            until = "next_schedule" if self.prefs["automatic"] else "further_notice"
        if until == "further_notice" and self.prefs["automatic"]:
            # Parking for good is the same as switching Automatik off.
            self.prefs["automatic"] = False
            await self.save_prefs()

        async def work(m: Mower) -> None:
            if until == "further_notice":
                await ask(m, "ClearOverride")
                await ask(m, "SetMode", mode=logic.MODE_HOME)
            else:
                await ask(m, "SetMode", mode=logic.MODE_AUTO)
                await ask(m, "SetOverrideParkUntilNextStart")
            await ask(m, "StartTrigger")

        await self.command(work)

    async def set_automatic(self, on: bool) -> None:
        self.prefs["automatic"] = on
        await self.save_prefs()
        if not self.link.in_range():
            # Applied on next contact (the rule in _enforce_mode).
            self.async_update_listeners()
            return

        async def work(m: Mower) -> None:
            if on:
                if await ask(m, "GetMode") == logic.MODE_HOME:
                    await ask(m, "SetMode", mode=logic.MODE_AUTO)
                    await ask(m, "StartTrigger")
            else:
                override = logic.override_from_raw(await ask(m, "GetOverride"))
                if not (override.action == logic.OVERRIDE_MOW and override.active(self.mower_now())):
                    await ask(m, "SetMode", mode=logic.MODE_HOME)
                    await ask(m, "StartTrigger")

        try:
            await self.command(work)
        except HomeAssistantError as err:
            LOGGER.info("Automatik gespeichert, wird beim nächsten Kontakt gesetzt (%s)", err)
            self.async_update_listeners()

    async def resync_schedule(self) -> None:
        self._schedule_attempts = 0
        self._tasks_at = 0.0
        await self.async_request_refresh()
