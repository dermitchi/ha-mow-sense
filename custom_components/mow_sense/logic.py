"""Pure helpers for Mow Sense (no Home Assistant imports, unit-tested)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

# Values as used by automower_ble.protocol (copied so this module stays pure).
STATE_OFF = 0
STATE_WAIT_FOR_SAFETYPIN = 1
STATE_STOPPED = 2
STATE_FATAL_ERROR = 3
STATE_PENDING_START = 4
STATE_PAUSED = 5
STATE_IN_OPERATION = 6
STATE_RESTRICTED = 7
STATE_ERROR = 8

ACT_NONE = 0
ACT_CHARGING = 1
ACT_GOING_OUT = 2
ACT_MOWING = 3
ACT_GOING_HOME = 4
ACT_PARKED = 5
ACT_STOPPED_IN_GARDEN = 6

MODE_AUTO = 0
MODE_MANUAL = 1
MODE_HOME = 2
MODE_DEMO = 3
MODE_POI = 4

OVERRIDE_NONE = 0
OVERRIDE_PARK = 1
OVERRIDE_MOW = 2

STATES = {
    STATE_OFF: "off",
    STATE_WAIT_FOR_SAFETYPIN: "wait_for_pin",
    STATE_STOPPED: "stopped",
    STATE_FATAL_ERROR: "fatal_error",
    STATE_PENDING_START: "pending_start",
    STATE_PAUSED: "paused",
    STATE_IN_OPERATION: "in_operation",
    STATE_RESTRICTED: "restricted",
    STATE_ERROR: "error",
}
ACTIVITIES = {
    ACT_NONE: "none",
    ACT_CHARGING: "charging",
    ACT_GOING_OUT: "going_out",
    ACT_MOWING: "mowing",
    ACT_GOING_HOME: "going_home",
    ACT_PARKED: "parked",
    ACT_STOPPED_IN_GARDEN: "stopped_in_garden",
}
MODES = {
    MODE_AUTO: "auto",
    MODE_MANUAL: "manual",
    MODE_HOME: "home",
    MODE_DEMO: "demo",
    MODE_POI: "poi",
}

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAY_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
DAY_SECONDS = 86400
# AddTask sends the duration as uint16.
MAX_TASK_SECONDS = 0xFFFF


def lawn_mower_activity(state: int | None, activity: int | None) -> str | None:
    """Map mower state/activity to a LawnMowerActivity value."""
    if state is None or activity is None:
        return None
    if state == STATE_PAUSED:
        return "paused"
    if state in (STATE_ERROR, STATE_FATAL_ERROR):
        return "error"
    if state in (STATE_STOPPED, STATE_OFF, STATE_WAIT_FOR_SAFETYPIN):
        # Stopped in the garden needs a human; in the station it is just idle.
        return "error" if activity == ACT_STOPPED_IN_GARDEN else "docked"
    if state == STATE_PENDING_START and activity == ACT_NONE:
        return "docked"
    if activity in (ACT_GOING_OUT, ACT_MOWING):
        return "mowing"
    if activity == ACT_GOING_HOME:
        return "returning"
    if activity in (ACT_CHARGING, ACT_PARKED, ACT_NONE):
        return "docked"
    if activity == ACT_STOPPED_IN_GARDEN:
        return "error"
    return None


def is_active(state: int | None, activity: int | None) -> bool:
    """Mower is outside or about to move – poll faster."""
    if activity in (ACT_GOING_OUT, ACT_MOWING, ACT_GOING_HOME, ACT_STOPPED_IN_GARDEN):
        return True
    return state in (STATE_PAUSED, STATE_PENDING_START, STATE_ERROR, STATE_STOPPED)


def is_outside(activity: int | None) -> bool:
    return activity in (ACT_GOING_OUT, ACT_MOWING, ACT_GOING_HOME, ACT_STOPPED_IN_GARDEN)


# --- mower clock -----------------------------------------------------------
# The mower keeps "local time seconds since 1970" (wall clock as if it were UTC).


def to_mower_time(now: datetime) -> int:
    """Aware datetime -> mower clock value."""
    offset = now.utcoffset() or timedelta()
    return int(now.timestamp() + offset.total_seconds())


def from_mower_time(value: int | None, tz: tzinfo) -> datetime | None:
    """Mower clock value -> aware datetime in tz (None for 0)."""
    if not value:
        return None
    naive = datetime(1970, 1, 1) + timedelta(seconds=value)
    return naive.replace(tzinfo=tz)


@dataclass(frozen=True)
class Override:
    action: int = OVERRIDE_NONE
    start: int = 0
    duration: int = 0

    def active(self, mower_now: int) -> bool:
        if self.action == OVERRIDE_NONE:
            return False
        if not self.duration:
            # "park until next start" has no duration
            return True
        return self.start <= mower_now < self.start + self.duration


def override_from_raw(raw: dict | None) -> Override:
    if not raw:
        return Override()
    return Override(
        int(raw.get("action", 0)), int(raw.get("startTime", 0)), int(raw.get("duration", 0))
    )


# --- schedule --------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Task:
    """One mower schedule task: start (s after midnight), duration (s), weekdays."""

    start: int
    duration: int
    days: tuple[bool, ...] = field(default=(False,) * 7)  # Monday first

    def kwargs(self) -> dict[str, int]:
        """Arguments for the AddTask command."""
        d = self.days
        return {
            "start": self.start,
            "duration": self.duration,
            "useOnSunday": int(d[6]),
            "unknown_1": 0,
            "useOnMonday": int(d[0]),
            "useOnTuesday": int(d[1]),
            "useOnWednesday": int(d[2]),
            "useOnThursday": int(d[3]),
            "useOnFriday": int(d[4]),
            "useOnSaturday": int(d[5]),
            "unknown_2": 0,
        }

    def label(self) -> str:
        return f"{day_label(self.days)} {clock(self.start)}–{clock(self.start + self.duration)}"


def task_from_raw(raw: dict) -> Task:
    return Task(
        int(raw["start"]),
        int(raw["duration"]),
        tuple(bool(raw[f"useOn{d.capitalize()}"]) for d in DAYS),
    )


def clock(seconds: int) -> str:
    seconds %= DAY_SECONDS + 1
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def day_label(days: tuple[bool, ...]) -> str:
    """(True,)*5+(False,)*2 -> 'Mo–Fr'; scattered days -> 'Mo, Mi, Fr'."""
    idx = [i for i, on in enumerate(days) if on]
    if not idx:
        return "–"
    if len(idx) == 7:
        return "täglich"
    if len(idx) > 2 and idx == list(range(idx[0], idx[-1] + 1)):
        return f"{DAY_SHORT[idx[0]]}–{DAY_SHORT[idx[-1]]}"
    return ", ".join(DAY_SHORT[i] for i in idx)


def _seconds(value) -> int:
    """'07:30', '07:30:00' or a time object -> seconds; time.max -> 86400."""
    parts = [float(p) for p in str(value).split(":")]
    while len(parts) < 3:
        parts.append(0.0)
    total = round(parts[0] * 3600 + parts[1] * 60 + parts[2])
    return DAY_SECONDS if total >= DAY_SECONDS - 1 else total


def tasks_from_schedule(schedule: dict[str, list[dict]]) -> list[Task]:
    """HA schedule.get_schedule response for one entity -> merged mower tasks.

    Equal time windows on several days become one task. Windows longer than the
    uint16 duration limit are split.
    """
    windows: dict[tuple[int, int], list[bool]] = {}
    for day_idx, day in enumerate(DAYS):
        for block in schedule.get(day) or []:
            start = _seconds(block["from"])
            end = _seconds(block["to"])
            if end == 0 or end <= start:
                end = DAY_SECONDS  # "00:00" end means midnight
            pos = start
            while pos < end:
                dur = min(end - pos, MAX_TASK_SECONDS)
                windows.setdefault((pos, dur), [False] * 7)[day_idx] = True
                pos += dur
    return sorted(Task(s, d, tuple(days)) for (s, d), days in windows.items())


def same_tasks(a: list[Task], b: list[Task]) -> bool:
    """Compare schedules regardless of how days are grouped into tasks."""
    return _expand(a) == _expand(b)


def _expand(tasks: list[Task]) -> set[tuple[int, int, int]]:
    out = set()
    for t in tasks:
        for i, on in enumerate(t.days):
            if on and t.duration > 0:
                out.add((i, t.start, t.duration))
    return out


def schedule_summary(tasks: list[Task]) -> str:
    if not tasks:
        return "kein Zeitplan"
    text = "; ".join(t.label() for t in sorted(tasks, key=lambda t: (t.days.index(True) if True in t.days else 9, t.start)))
    return text if len(text) <= 255 else text[:252] + "…"


def error_key(code: int | None, names: dict[int, str]) -> str | None:
    """Error code -> lower-case name (None when no error; 0 means none)."""
    if not code:
        return None
    name = names.get(code)
    return name.lower() if name else f"code_{code}"


ERROR_DE = {
    "outside_working_area": "Außerhalb des Arbeitsbereichs",
    "no_loop_signal": "Kein Begrenzungsdraht-Signal",
    "wrong_loop_signal": "Falsches Drahtsignal",
    "wrong_pin_code": "Falscher PIN",
    "trapped": "Festgefahren",
    "upside_down": "Umgekippt",
    "low_battery": "Akku schwach",
    "empty_battery": "Akku leer",
    "no_drive": "Kein Antrieb",
    "mower_lifted": "Angehoben",
    "lifted": "Angehoben",
    "stuck_in_charging_station": "Hängt in der Ladestation",
    "charging_station_blocked": "Ladestation blockiert",
    "wheel_motor_blocked_right": "Rechtes Rad blockiert",
    "wheel_motor_blocked_left": "Linkes Rad blockiert",
    "cutting_system_blocked": "Messer blockiert",
    "cutting_system_blocked_2": "Messer blockiert",
    "slope_too_steep": "Hang zu steil",
    "charging_system_problem": "Problem beim Laden",
    "mower_tilted": "Schräglage",
    "cutting_stopped_slope_too_steep": "Mähen gestoppt – Hang zu steil",
    "no_response_from_charger": "Ladestation antwortet nicht",
    "difficult_finding_home": "Findet die Station schwer",
    "battery_problem": "Akkuproblem",
    "alarm_mower_switched_off": "Alarm: ausgeschaltet",
    "alarm_mower_stopped": "Alarm: gestoppt",
    "alarm_mower_lifted": "Alarm: angehoben",
    "alarm_mower_tilted": "Alarm: gekippt",
    "no_power_in_charging_station": "Kein Strom an der Ladestation",
    "switch_cord_problem": "Problem mit dem Schaltkabel",
    "high_internal_temperature": "Zu warm",
    "battery_needs_replacement": "Akku muss getauscht werden",
    "battery_near_end_of_life": "Akku bald verbraucht",
    "boundary_wire_problem": "Problem mit dem Begrenzungsdraht",
}


def error_text(code: int | None, names: dict[int, str]) -> str:
    key = error_key(code, names)
    if key is None:
        return "Kein Fehler"
    if key in ERROR_DE:
        return ERROR_DE[key]
    if key.startswith("temporary_battery_problem"):
        return "Vorübergehendes Akkuproblem"
    return key.replace("_", " ").capitalize()
