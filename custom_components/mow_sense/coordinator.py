"""Polling, last-known values and the 'HA is in charge' rules."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import time
from typing import Any

from automower_ble.error_codes import ErrorCodes
from automower_ble.models import MowerModels
from automower_ble.mower import Mower
from habluetooth import BaseHaRemoteScanner

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import logic
from .client import AuthError, LinkError, MowerLink, NotInRange, ask
from .const import (
    CLOCK_TOLERANCE,
    CONF_ACTIVE_INTERVAL,
    CONF_GRACE_MINUTES,
    CONF_IDLE_INTERVAL,
    CONF_MIN_RUN,
    CONF_MIN_TEMP,
    CONF_RAIN_DRY,
    CONF_RAIN_LEAD,
    CONF_SCHEDULE_ENTITY,
    CONF_WEATHER_ENTITY,
    DEFAULT_MIN_RUN,
    DEFAULT_MIN_TEMP,
    DEFAULT_RAIN_DRY,
    DEFAULT_RAIN_LEAD,
    FORECAST_EVERY,
    DEFAULT_ACTIVE_INTERVAL,
    DEFAULT_BLADE_HOURS,
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
    firmware: str | None = None

    @property
    def cutting_hours(self) -> float | None:
        secs = self.stats.get("totalCuttingTime")
        return secs / 3600 if secs is not None else None


def _data_to_dict(d: MowerData) -> dict[str, Any]:
    return {
        "fetched_at": d.fetched_at.isoformat(),
        "battery": d.battery, "charging": d.charging, "state": d.state, "activity": d.activity,
        "mode": d.mode, "error_code": d.error_code,
        "next_start": d.next_start.isoformat() if d.next_start else None,
        "tasks": [[t.start, t.duration, list(t.days)] for t in d.tasks] if d.tasks is not None else None,
        "stats": d.stats, "model": d.model, "serial": d.serial, "name": d.name, "firmware": d.firmware,
    }


def _data_from_dict(raw: dict[str, Any]) -> MowerData:
    d = MowerData(fetched_at=dt_util.parse_datetime(raw["fetched_at"]))
    for key in ("battery", "charging", "state", "activity", "mode", "error_code", "model", "serial", "name", "firmware"):
        setattr(d, key, raw.get(key))
    d.stats = raw.get("stats") or {}
    d.next_start = dt_util.parse_datetime(raw["next_start"]) if raw.get("next_start") else None
    if raw.get("tasks") is not None:
        d.tasks = [logic.Task(s, du, tuple(days)) for s, du, days in raw["tasks"]]
    return d


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
        self.prefs: dict[str, Any] = {
            "automatic": True,
            "mow_hours": DEFAULT_MOW_HOURS,
            "blade_interval": DEFAULT_BLADE_HOURS,
            "blade_changed": None,  # ISO time of the last blade change
            "blade_base": None,  # total cutting hours at that change
            "pending": None,  # {"until": iso} – HA wants a run, waiting for dry weather
            "last_rain_end": None,  # iso
            "proxy": None,  # source of the Bluetooth proxy that last heard the mower
        }
        self.rain = logic.RainState()
        self._forecast_at = 0.0
        self._spells: list = []
        self.frost = False
        self._slow_at = float("-inf")  # first poll always reads statistics
        self._tasks_at = float("-inf")
        self._schedule_attempts = 0
        self._wanted_tasks: list[logic.Task] | None = None
        self._unsub_ble = None

    # --- setup ---------------------------------------------------------------

    async def async_setup(self) -> None:
        if stored := await self._store.async_load():
            last = stored.pop("last", None)
            self.prefs.update(stored)
            if last:
                self.data = _data_from_dict(last)
                self.last_contact = self.data.fetched_at
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
        self._remember_proxy(info.source)
        if not self.in_range and self.link.in_range():
            # Back from being switched off / out of range: poll right away.
            self.in_range = True
            LOGGER.debug("Mäher wieder in Reichweite")
            self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_unavailable(self, info) -> None:
        self.in_range = False
        self.connected = False
        self.async_update_listeners()

    @callback
    def _remember_proxy(self, source: str | None) -> None:
        scanner = bluetooth.async_scanner_by_source(self.hass, source) if source else None
        if isinstance(scanner, BaseHaRemoteScanner) and self.prefs["proxy"] != source:
            self.prefs["proxy"] = source
            self._store.async_delay_save(self._stored, 10)

    def proxy_online(self) -> bool:
        if source := self.prefs["proxy"]:
            return bluetooth.async_scanner_by_source(self.hass, source) is not None
        # Proxy not known yet: any connectable proxy counts.
        return any(
            isinstance(s, BaseHaRemoteScanner) and s.connectable
            for s in bluetooth.async_current_scanners(self.hass)
        )

    def proxy_name(self) -> str | None:
        source = self.prefs["proxy"]
        scanner = bluetooth.async_scanner_by_source(self.hass, source) if source else None
        return scanner.name if scanner else source

    def reachability(self) -> str:
        """online | no_answer | mower_off | proxy_offline"""
        if self.connected:
            return "online"
        if not self.proxy_online():
            return "proxy_offline"
        if not self.in_range:
            return "mower_off"
        return "no_answer"

    def offline_reason(self) -> str | None:
        """Shown instead of live values: right away when the proxy is gone,
        otherwise once the last contact is older than the grace time."""
        reason = self.reachability()
        if reason == "proxy_offline" or (reason != "online" and not self.fresh()):
            return reason
        return None

    async def save_prefs(self) -> None:
        await self._store.async_save(self._stored())

    def _stored(self) -> dict[str, Any]:
        # Last values survive restarts (the mower is off all winter).
        return {**self.prefs, "last": _data_to_dict(self.data) if self.data else None}

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
        if self.rain_enabled:
            await self._update_rain()  # forecast also while the mower is off
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
            # No UpdateFailed: entities show why the mower is offline instead.
            LOGGER.debug("Behalte letzte Werte: %s", err)
            return self.data
        self.connected = True
        self.last_problem = None
        self.last_contact = data.fetched_at
        if info := bluetooth.async_last_service_info(self.hass, self.link.address, connectable=True):
            self._remember_proxy(info.source)
        self._store.async_delay_save(self._stored, 60)
        self._update_device(data)
        self._set_interval(data)
        return data

    def _update_device(self, data: MowerData) -> None:
        registry = dr.async_get(self.hass)
        devices = dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
        device = devices[0] if devices else None
        serial = str(data.serial) if data.serial else None
        if device and (
            device.model != data.model or device.serial_number != serial or device.sw_version != data.firmware
        ):
            registry.async_update_device(
                device.id, model=data.model, serial_number=serial, sw_version=data.firmware
            )

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
        if d.firmware is None:
            d.firmware = await ask(m, "GetSwVersionStringAppl") or None

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

        if self.prefs["blade_changed"] and self.prefs["blade_base"] is None and d.cutting_hours is not None:
            self.prefs["blade_base"] = round(d.cutting_hours, 2)
            await self.save_prefs()

        await self._rain_guard(m, d)
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
        if outside:
            # Send it home now. Never trigger a mower that is not outside: off
            # the station (e.g. stored in the cellar) that could make it move.
            await ask(m, "StartTrigger")
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
            if isinstance(raw, dict) and "useOnSunday" in raw:
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
            self._tasks_at = float("-inf")
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

    async def start_mowing(self, hours: float | None = None, ignore_rain: bool = False) -> None:
        hours = hours or self.prefs["mow_hours"]
        if not ignore_rain and self.rain_enabled:
            await self._update_rain()
            now = dt_util.utcnow()
            if not self.rain.can_start(now, self.min_run):
                # Rain now/soon: remember the wish, start once it is dry and still worth it.
                self.prefs["pending"] = {"until": (now + timedelta(hours=hours)).isoformat()}
                await self.save_prefs()
                LOGGER.info("Regen – Start wartet bis %s", self.prefs["pending"]["until"])
                self._correction("rain_wait", "Regen angesagt: Start wartet auf trockenes Wetter", False)
                self.async_update_listeners()
                return
        self.prefs["pending"] = None
        await self.save_prefs()
        await self.command(self._start_work(hours))

    def _start_work(self, hours: float):
        async def work(m: Mower) -> None:
            state = await ask(m, "GetState")
            activity = await ask(m, "GetActivity")
            if state == logic.STATE_PAUSED and logic.is_outside(activity):
                await ask(m, "StartTrigger")  # resume where it stopped
                return
            await ask(m, "SetMode", mode=logic.MODE_AUTO)
            await ask(m, "SetOverrideMow", duration=int(hours * 3600))
            await ask(m, "StartTrigger")

        return work

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
            # Only the mode is changed; no StartTrigger, so nothing moves by itself.
            if on:
                if await ask(m, "GetMode") == logic.MODE_HOME:
                    await ask(m, "SetMode", mode=logic.MODE_AUTO)
                return
            override = logic.override_from_raw(await ask(m, "GetOverride"))
            if override.action == logic.OVERRIDE_MOW and override.active(self.mower_now()):
                return  # a run started on purpose finishes first
            await ask(m, "SetMode", mode=logic.MODE_HOME)
            if logic.is_outside(await ask(m, "GetActivity")):
                await ask(m, "StartTrigger")  # out in the garden: go home now

        try:
            await self.command(work)
        except HomeAssistantError as err:
            LOGGER.info("Automatik gespeichert, wird beim nächsten Kontakt gesetzt (%s)", err)
            self.async_update_listeners()

    async def resync_schedule(self) -> None:
        self._schedule_attempts = 0
        self._tasks_at = float("-inf")
        await self.async_request_refresh()

    # --- blades --------------------------------------------------------------------

    def blade_hours(self) -> float | None:
        base = self.prefs["blade_base"]
        hours = self.data.cutting_hours if self.data else None
        if base is None or hours is None:
            return None
        return round(max(0.0, hours - base), 1)

    def blade_due(self) -> bool | None:
        used = self.blade_hours()
        return None if used is None else used >= self.prefs["blade_interval"]

    async def blades_changed(self) -> None:
        """Remember the change; counted in HA, the mower is not touched."""
        hours = self.data.cutting_hours if self.data else None
        self.prefs["blade_changed"] = dt_util.utcnow().isoformat()
        self.prefs["blade_base"] = round(hours, 2) if hours is not None else None
        await self.save_prefs()
        self.async_update_listeners()

    # --- rain guard ------------------------------------------------------------------

    @property
    def rain_enabled(self) -> bool:
        return bool(self._opt(CONF_WEATHER_ENTITY, None))

    @property
    def min_run(self) -> timedelta:
        return timedelta(minutes=self._opt(CONF_MIN_RUN, DEFAULT_MIN_RUN))

    async def _update_rain(self) -> None:
        entity_id = self._opt(CONF_WEATHER_ENTITY, None)
        if not entity_id:
            self.rain = logic.RainState()
            return
        if time.monotonic() - self._forecast_at >= FORECAST_EVERY or not self._forecast_at:
            try:
                resp = await self.hass.services.async_call(
                    "weather", "get_forecasts", {"type": "hourly"},
                    target={"entity_id": entity_id}, blocking=True, return_response=True,
                )
                self._spells = logic.rain_spells(
                    resp[entity_id]["forecast"], dt_util.parse_datetime, self._min_temp()
                )
                self._forecast_at = time.monotonic()
            except (HomeAssistantError, KeyError) as err:
                LOGGER.warning("Wettervorhersage nicht lesbar: %s", err)
        state = self.hass.states.get(entity_id)
        self.frost = state is not None and logic.is_cold(
            state.attributes.get("temperature"), state.state, self._min_temp()
        )
        raining_now = state is not None and state.state in logic.RAIN_CONDITIONS or self.frost
        now = dt_util.utcnow()
        last_end = self.prefs["last_rain_end"]
        new = logic.rain_state(
            now, raining_now, self._spells,
            dt_util.parse_datetime(last_end) if last_end else None,
            timedelta(minutes=self._opt(CONF_RAIN_LEAD, DEFAULT_RAIN_LEAD)),
            timedelta(minutes=self._opt(CONF_RAIN_DRY, DEFAULT_RAIN_DRY)),
        )
        if self.rain.raining and not new.raining:
            self.prefs["last_rain_end"] = now.isoformat()
            await self.save_prefs()
            new = logic.rain_state(
                now, False, self._spells, now,
                timedelta(minutes=self._opt(CONF_RAIN_LEAD, DEFAULT_RAIN_LEAD)),
                timedelta(minutes=self._opt(CONF_RAIN_DRY, DEFAULT_RAIN_DRY)),
            )
        self.rain = new

    def _min_temp(self) -> float | None:
        value = self._opt(CONF_MIN_TEMP, DEFAULT_MIN_TEMP)
        return None if value is None or value <= -20 else float(value)

    def rain_status(self) -> str:
        if not self.rain_enabled:
            return "off"
        if self.frost:
            return "frost"
        return self.rain.status(dt_util.utcnow(), timedelta(minutes=self._opt(CONF_RAIN_LEAD, DEFAULT_RAIN_LEAD)))

    async def _rain_guard(self, m: Mower, d: MowerData) -> None:
        if not self.rain_enabled:
            return
        await self._update_rain()
        now = dt_util.utcnow()
        r = self.rain
        park_until = r.blocked_until or (r.rain_end + timedelta(minutes=self._opt(CONF_RAIN_DRY, DEFAULT_RAIN_DRY)) if r.rain_end else None)
        mower_now = self.mower_now()

        # 1. Out mowing and rain is (nearly) here: home now, resume later.
        if d.activity in (logic.ACT_GOING_OUT, logic.ACT_MOWING) and r.must_go_home(now):
            if d.override.action == logic.OVERRIDE_MOW and d.override.active(mower_now):
                end = now + timedelta(seconds=d.override.start + d.override.duration - mower_now)
                self.prefs["pending"] = {"until": end.isoformat()}
                await self.save_prefs()
            if self.prefs["automatic"] and park_until:
                await ask(m, "SetOverridePark", duration=max(60, int((park_until - now).total_seconds())))
            else:
                await ask(m, "SetMode", mode=logic.MODE_HOME)
                d.mode = logic.MODE_HOME
            await ask(m, "StartTrigger")
            self._correction("rain", "Regen/Frost: Rasi fährt heim", True)
            return

        # 2. In the station on its own schedule: keep it there while it rains / is wet.
        docked = d.activity in (logic.ACT_CHARGING, logic.ACT_PARKED)
        if (
            docked
            and self.prefs["automatic"]
            and park_until
            and not r.can_start(now, self.min_run)
            and d.override.action != logic.OVERRIDE_PARK
            and d.next_start is not None
            and d.next_start <= park_until
        ):
            await ask(m, "SetOverridePark", duration=max(60, int((park_until - now).total_seconds())))
            self._correction("rain", "Regen/Frost: Rasi bleibt in der Station", False)
            return

        # 3. HA wanted a run (or one was interrupted): start once dry and still worth it.
        pending = self.prefs["pending"]
        if pending:
            until = dt_util.parse_datetime(pending["until"])
            if until - now < self.min_run:
                self.prefs["pending"] = None
                await self.save_prefs()
                self._correction("rain_gave_up", "Regen: Mähen heute ausgefallen, lohnt sich nicht mehr", False)
            elif docked and r.can_start(now, self.min_run):
                hours = round((until - now).total_seconds() / 3600, 2)
                self.prefs["pending"] = None
                await self.save_prefs()
                await self._start_work(hours)(m)
                self._correction("rain_resume", f"Trocken: Rasi mäht noch {hours:.1f} h", False)
