"""End-to-end against a simulated mower (no real Bluetooth)."""

from __future__ import annotations

import asyncio
from importlib.resources import files
import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock, patch

from automower_ble.protocol import ResponseResult
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.mow_sense import logic
from custom_components.mow_sense.const import DOMAIN, EVENT_CORRECTED
from custom_components.mow_sense.frames import FIXED, SIZES

ADDR = "AA:BB:CC:DD:EE:FF"


PROTOCOL = json.loads((files("automower_ble") / "protocol.json").read_text())
BY_ID = {(v["major"], v["minor"]): k for k, v in PROTOCOL.items()}
for _name, _spec in FIXED.items():
    BY_ID[(_spec["major"], _spec["minor"])] = _name


def _encode(request, rtype, value) -> bytearray:
    data = bytearray()
    if rtype is not None and value is not None:
        fields = rtype if isinstance(rtype, dict) else {"response": rtype}
        values = value if isinstance(rtype, dict) else {"response": value}
        for key, kind in fields.items():
            v = values.get(key, 0)
            if kind == "ascii":
                data += v.encode() + b"\0"
            elif kind == "remaining_uint":
                data += b"\0\0"
            else:
                data += int(v).to_bytes(SIZES[kind], "little")
    resp = bytearray(request[:18])
    resp[10] = 0x01
    resp[16] = 0
    resp[17:19] = len(data).to_bytes(2, "little")
    return resp + data + b"\0\x03"


class FakeMower:
    """Answers like automower_ble.Mower.command."""

    instances: list["FakeMower"] = []
    state: dict = {}
    log: list = []
    connect_result = ResponseResult.OK

    def __init__(self, channel_id, address, pin=None):
        self.channel_id = channel_id
        self.keep_alive_event = asyncio.Event()
        self.client = None
        self._connected = False
        FakeMower.instances.append(self)

    async def connect(self, device):
        if FakeMower.connect_result is ResponseResult.OK:
            self._connected = True
            self.client = object()
        return FakeMower.connect_result

    def is_connected(self):
        return self._connected

    async def get_protocol(self):
        return PROTOCOL

    async def _request_response(self, request):
        """Decode our frame, answer via command(), encode a response frame."""
        major = request[12] | request[13] << 8
        name = BY_ID[(major, request[14])]
        spec = FIXED.get(name) or PROTOCOL[name]
        payload, kw, pos = request[18:-2], {}, 0
        for key, kind in (spec.get("requestType") or {}).items():
            size = SIZES[kind]
            kw[key] = int.from_bytes(payload[pos:pos + size], "little")
            pos += size
        value = await self.command(name, **kw)
        return _encode(request, spec.get("responseType"), value)

    async def disconnect(self):
        self._connected = False

    async def command(self, name, **kw):
        s = FakeMower.state
        FakeMower.log.append((name, kw))
        simple = {
            "GetModel": {"deviceType": 29, "deviceVariant": 4},
            "GetSerialNumber": 123456,
            "GetUserMowerNameAsAsciiString": "SILENO minimo 250",
            "GetBatteryLevel": s["battery"],
            "IsCharging": s["charging"],
            "GetState": s["state"],
            "GetActivity": s["activity"],
            "GetMode": s["mode"],
            "GetError": 0,
            "GetOverride": {"action": s["override"][0], "startTime": s["override"][1], "duration": s["override"][2]},
            "GetNextStartTime": s.get("next_start", 0),
            "GetRemainingChargingTime": 1200,
            "GetAllStatistics": {"totalRunningTime": 36000, "totalCuttingTime": 30000, "totalChargingTime": 1,
                                 "totalSearchingTime": 1, "numberOfCollisions": 5, "numberOfChargingCycles": 7,
                                 "cuttingBladeUsageTime": 18000},
            "GetTime": s.get("clock", logic.to_mower_time(dt_util.now())),
            "GetNumberOfTasks": len(s["tasks"]),
            "GetMessage": {"time": 0, "code": 0, "severity": 0},
        }
        if name in simple:
            return simple[name]
        if name == "GetTask":
            t = s["tasks"][kw["taskId"]]
            raw = {"start": t.start, "duration": t.duration}
            for i, d in enumerate(logic.DAYS):
                raw[f"useOn{d.capitalize()}"] = int(t.days[i])
            return raw
        if name == "SetMode":
            s["mode"] = kw["mode"]
        elif name == "DeleteAllTask":
            s["pending"] = []
        elif name == "AddTask":
            days = tuple(bool(kw[f"useOn{d.capitalize()}"]) for d in logic.DAYS)
            s["pending"].append(logic.Task(kw["start"], kw["duration"], days))
        elif name == "CommitTaskTransaction":
            s["tasks"] = s.pop("pending")
        elif name == "SetOverridePark":
            s["override"] = (logic.OVERRIDE_PARK, logic.to_mower_time(dt_util.now()), kw["duration"])
        elif name == "SetOverrideMow":
            s["override"] = (logic.OVERRIDE_MOW, logic.to_mower_time(dt_util.now()), kw["duration"])
        return None


@pytest.fixture(autouse=True)
def fake_ble():
    FakeMower.instances = []
    FakeMower.log = []
    FakeMower.connect_result = ResponseResult.OK
    FakeMower.state = {
        "battery": 80, "charging": True, "state": logic.STATE_RESTRICTED, "activity": logic.ACT_PARKED,
        "mode": logic.MODE_AUTO, "override": (0, 0, 0), "tasks": [],
    }
    present = {"on": True}
    with (
        patch("custom_components.mow_sense.client.Mower", FakeMower),
        patch("homeassistant.components.bluetooth.async_ble_device_from_address",
              side_effect=lambda *a, **k: MagicMock() if present["on"] else None),
        patch("homeassistant.components.bluetooth.async_address_present", side_effect=lambda *a, **k: present["on"]),
        patch("homeassistant.components.bluetooth.async_register_callback", return_value=lambda: None),
        patch("homeassistant.components.bluetooth.async_track_unavailable", return_value=lambda: None),
        patch("custom_components.mow_sense.client.close_stale_connections_by_address"),
    ):
        yield present


async def _setup(hass: HomeAssistant, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="SILENO Minimo 250", unique_id=ADDR,
        data={"address": ADDR, "channel_id": 4711, "pin": ""}, options=options or {},
    )
    entry.add_to_hass(hass)
    hass.config.components.add("bluetooth_adapters")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _cmds():
    return [c for c, _ in FakeMower.log]


async def test_poll_and_entities(hass):
    await _setup(hass)
    assert hass.states.get("lawn_mower.sileno_minimo_250").state == "docked"
    assert hass.states.get("sensor.sileno_minimo_250_battery").state == "80"
    assert hass.states.get("sensor.sileno_minimo_250_blade_usage").state == "5.0"
    assert hass.states.get("sensor.sileno_minimo_250_error").state == "Kein Fehler"
    assert all(not m.is_connected() for m in FakeMower.instances), "session must disconnect"


async def test_keeps_values_when_out_of_range(hass, fake_ble):
    await _setup(hass)
    fake_ble["on"] = False
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=10))
    await hass.async_block_till_done()
    assert hass.states.get("lawn_mower.sileno_minimo_250").state == "docked"
    # after the grace time the live state goes unavailable
    with patch("homeassistant.util.dt.utcnow", return_value=dt_util.utcnow() + timedelta(minutes=45)):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=45))
        await hass.async_block_till_done()
        assert hass.states.get("lawn_mower.sileno_minimo_250").state == "unavailable"


async def test_setup_when_mower_off(hass, fake_ble):
    fake_ble["on"] = False
    entry = await _setup(hass)
    assert entry.state.name == "LOADED"
    assert hass.states.get("lawn_mower.sileno_minimo_250").state == "unavailable"
    assert hass.states.get("switch.sileno_minimo_250_automatic_own_schedule").state == "on"
    assert hass.states.get("binary_sensor.sileno_minimo_250_in_bluetooth_range").state == "off"


async def test_automatic_off_stops_schedule_runs(hass):
    await _setup(hass)
    events = []
    hass.bus.async_listen(EVENT_CORRECTED, lambda e: events.append(e.data))
    sw = next(s.entity_id for s in hass.states.async_all("switch"))
    await hass.services.async_call("switch", "turn_off", {"entity_id": sw}, blocking=True)
    assert FakeMower.state["mode"] == logic.MODE_HOME

    # someone (Gardena app) flips it back to AUTO and it drives off
    FakeMower.state.update(mode=logic.MODE_AUTO, state=logic.STATE_IN_OPERATION, activity=logic.ACT_MOWING)
    FakeMower.log.clear()
    await hass.config_entries.async_entries(DOMAIN)[0].runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert FakeMower.state["mode"] == logic.MODE_HOME
    assert ("StartTrigger", {}) in FakeMower.log
    assert events and events[-1]["was_driving"]


async def test_ha_start_survives_automatic_off(hass):
    entry = await _setup(hass)
    c = entry.runtime_data
    await c.set_automatic(False)
    await hass.services.async_call("lawn_mower", "start_mowing", {"entity_id": "lawn_mower.sileno_minimo_250"}, blocking=True)
    assert FakeMower.state["mode"] == logic.MODE_AUTO
    assert FakeMower.state["override"][2] == 3 * 3600
    FakeMower.state.update(state=logic.STATE_IN_OPERATION, activity=logic.ACT_MOWING)
    await c.async_refresh()
    assert FakeMower.state["mode"] == logic.MODE_AUTO, "own run must not be cancelled"
    # override over -> back to park
    FakeMower.state["override"] = (0, 0, 0)
    await c.async_refresh()
    assert FakeMower.state["mode"] == logic.MODE_HOME


async def test_schedule_helper_is_written(hass):
    assert await async_setup_component(hass, "schedule", {"schedule": {"maeher": {
        "name": "Mäher", "monday": [{"from": "10:00", "to": "12:00"}], "friday": [{"from": "10:00", "to": "12:00"}],
    }}})
    FakeMower.state["tasks"] = [logic.Task(0, 3600 * 8, (True,) * 7)]  # app-made
    entry = await _setup(hass, {"schedule_entity": "schedule.maeher"})
    want = [logic.Task(36000, 7200, (True, False, False, False, True, False, False))]
    assert logic.same_tasks(FakeMower.state["tasks"], want)
    assert entry.runtime_data.schedule_status == "written"
    n = _cmds().count("AddTask")
    await entry.runtime_data.async_refresh()
    assert _cmds().count("AddTask") == n, "no rewrite when in sync"


async def test_park_further_notice_turns_automatic_off(hass):
    entry = await _setup(hass)
    await hass.services.async_call(DOMAIN, "park", {"entity_id": "lawn_mower.sileno_minimo_250", "until": "further_notice"}, blocking=True)
    assert FakeMower.state["mode"] == logic.MODE_HOME
    assert entry.runtime_data.prefs["automatic"] is False


async def test_wrong_pin_starts_reauth(hass):
    FakeMower.connect_result = ResponseResult.INVALID_PIN
    entry = await _setup(hass)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"].get("source") == "reauth" for f in flows)


async def test_config_flow_manual(hass):
    hass.config.components.add("bluetooth_adapters")
    with patch("homeassistant.components.bluetooth.async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["type"] == "form"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": ADDR.lower(), "pin": "12ab"})
        assert result["errors"] == {"pin": "invalid_pin"}
        with patch("custom_components.mow_sense.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": ADDR, "pin": "1234"})
    assert result["type"] == "create_entry"
    assert result["title"] == "SILENO minimo 250"
    assert result["data"]["pin"] == "1234" and result["data"]["address"] == ADDR
    assert 1 <= result["data"]["channel_id"] <= 0xFFFFFFFF


async def test_options_flow(hass):
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"active_interval": 60, "idle_interval": 600, "grace_minutes": 30}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["idle_interval"] == 600


async def test_cellar_never_gets_start_trigger(hass):
    """Real case 2026-10-22: switched on in the cellar, STOPPED/NONE, Automatik off."""
    FakeMower.state.update(state=logic.STATE_STOPPED, activity=logic.ACT_NONE, charging=False)
    entry = await _setup(hass)
    assert hass.states.get("lawn_mower.sileno_minimo_250").state == "error"
    FakeMower.log.clear()
    await entry.runtime_data.set_automatic(False)
    await entry.runtime_data.async_refresh()
    assert FakeMower.state["mode"] == logic.MODE_HOME
    assert "StartTrigger" not in _cmds()


async def test_blade_counter(hass):
    entry = await _setup(hass)
    c = entry.runtime_data
    assert hass.states.get("sensor.sileno_minimo_250_blades_since_change").state == "unavailable"
    await hass.services.async_call("button", "press", {"entity_id": "button.sileno_minimo_250_blades_changed"}, blocking=True)
    assert c.prefs["blade_base"] == round(30000 / 3600, 2)
    assert hass.states.get("sensor.sileno_minimo_250_blades_since_change").state == "0.0"
    assert hass.states.get("binary_sensor.sileno_minimo_250_blade_change_due").state == "off"
    # 120 cutting hours later
    stats = dict(c.data.stats, totalCuttingTime=30000 + 120 * 3600)
    c.async_set_updated_data(replace(c.data, stats=stats))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.sileno_minimo_250_blades_since_change").state == "120.0"
    assert hass.states.get("binary_sensor.sileno_minimo_250_blade_change_due").state == "on"
    await hass.services.async_call("number", "set_value", {"entity_id": "number.sileno_minimo_250_change_blades_every", "value": 150}, blocking=True)
    assert hass.states.get("binary_sensor.sileno_minimo_250_blade_change_due").state == "off"
    assert hass.states.get("sensor.sileno_minimo_250_blades_since_change").attributes["remaining"] == 30.0


def _weather(hass, rainy_hours_from_now=(), now_state="sunny"):
    """Fake hourly forecast: list of hour offsets with rain."""
    from homeassistant.core import SupportsResponse

    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    hass.states.async_set("weather.test", now_state)

    async def get_forecasts(call):
        return {"weather.test": {"forecast": [
            {"datetime": (base + timedelta(hours=h)).isoformat(), "condition": "rainy" if h in rainy_hours_from_now else "sunny",
             "precipitation": 2.0 if h in rainy_hours_from_now else 0.0} for h in range(24)]}}

    hass.services.async_register("weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)


RAIN_OPTS = {"weather_entity": "weather.test", "rain_lead_minutes": 30, "rain_dry_minutes": 60, "min_run_minutes": 45}


async def test_rain_sends_mower_home(hass):
    _weather(hass, rainy_hours_from_now=(0, 1), now_state="rainy")
    FakeMower.state.update(state=logic.STATE_IN_OPERATION, activity=logic.ACT_MOWING, charging=False)
    entry = await _setup(hass, RAIN_OPTS)
    assert ("StartTrigger", {}) in FakeMower.log
    assert any(c == "SetOverridePark" for c in _cmds())  # Automatik on: park, schedule resumes later
    assert hass.states.get("sensor.sileno_minimo_250_rain_guard").state == "raining"


async def test_start_waits_for_dry_weather_then_resumes(hass):
    _weather(hass, rainy_hours_from_now=(0,), now_state="rainy")
    entry = await _setup(hass, RAIN_OPTS)
    c = entry.runtime_data
    FakeMower.log.clear()
    await c.start_mowing(4)
    assert "SetOverrideMow" not in _cmds()
    assert c.prefs["pending"]
    # rain over, dry time passed
    _weather(hass, rainy_hours_from_now=())
    c._forecast_at = 0
    c.prefs["last_rain_end"] = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    c.rain = logic.RainState()
    await c.async_refresh()
    assert "SetOverrideMow" in _cmds()
    assert c.prefs["pending"] is None


async def test_no_start_if_rain_soon(hass):
    _weather(hass, rainy_hours_from_now=(1,))  # rain in < 1 h: leave_by within 30 min
    entry = await _setup(hass, RAIN_OPTS)
    FakeMower.log.clear()
    await entry.runtime_data.start_mowing(3)
    assert "SetOverrideMow" not in _cmds()
    await entry.runtime_data.start_mowing(3, ignore_rain=True)
    assert "SetOverrideMow" in _cmds()


async def test_station_keeps_mower_when_schedule_start_in_rain(hass):
    _weather(hass, rainy_hours_from_now=(0, 1), now_state="rainy")
    FakeMower.state["next_start"] = logic.to_mower_time(dt_util.now() + timedelta(minutes=20))
    entry = await _setup(hass, RAIN_OPTS)
    assert "SetOverridePark" in _cmds()
    assert "StartTrigger" not in _cmds()
