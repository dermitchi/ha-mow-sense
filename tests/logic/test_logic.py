from datetime import datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo

import logic as L


def test_schedule_groups_equal_windows_across_days():
    sched = {d: [] for d in L.DAYS}
    for d in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        sched[d] = [{"from": "10:00:00", "to": "12:30:00"}]
    sched["saturday"] = [{"from": time(9, 0), "to": time(11, 0)}]
    tasks = L.tasks_from_schedule(sched)
    assert len(tasks) == 2
    week = next(t for t in tasks if t.start == 36000)
    assert week.duration == 9000
    assert week.days == (True,) * 5 + (False, False)
    assert L.day_label(week.days) == "Mo–Fr"
    assert week.label() == "Mo–Fr 10:00–12:30"


def test_whole_day_is_split_for_uint16():
    sched = {"sunday": [{"from": time(0, 0), "to": time.max}]}
    tasks = L.tasks_from_schedule(sched)
    assert sum(t.duration for t in tasks) == 86400
    assert all(t.duration <= 0xFFFF for t in tasks)
    assert all(t.days[6] and not any(t.days[:6]) for t in tasks)


def test_midnight_end():
    tasks = L.tasks_from_schedule({"monday": [{"from": "22:00", "to": "00:00:00"}]})
    assert tasks == [L.Task(79200, 7200, (True,) + (False,) * 6)]


def test_same_tasks_ignores_grouping():
    a = [L.Task(3600, 600, (True, True) + (False,) * 5)]
    b = [L.Task(3600, 600, (True,) + (False,) * 6), L.Task(3600, 600, (False, True) + (False,) * 5)]
    assert L.same_tasks(a, b)
    assert not L.same_tasks(a, b[:1])
    assert L.same_tasks([], [])


def test_addtask_kwargs_order():
    k = L.Task(100, 200, (True, False, False, False, False, False, True)).kwargs()
    assert k["useOnMonday"] == 1 and k["useOnSunday"] == 1 and k["useOnTuesday"] == 0
    assert list(k)[:4] == ["start", "duration", "useOnSunday", "unknown_1"]


def test_task_from_raw_roundtrip():
    raw = {"start": 100, "duration": 200, "useOnMonday": 1, "useOnTuesday": 0, "useOnWednesday": 0,
           "useOnThursday": 0, "useOnFriday": 1, "useOnSaturday": 0, "useOnSunday": 0, "unknown": 0}
    assert L.task_from_raw(raw) == L.Task(100, 200, (True, False, False, False, True, False, False))


def test_mower_clock_is_local_wall_time():
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 7, 1, 14, 0, tzinfo=berlin)  # 12:00 UTC
    v = L.to_mower_time(now)
    assert v == int(datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc).timestamp())
    back = L.from_mower_time(v, berlin)
    assert back == now
    assert L.from_mower_time(0, berlin) is None


def test_override_active():
    o = L.Override(L.OVERRIDE_MOW, 1000, 3600)
    assert o.active(2000) and not o.active(5000) and not o.active(999)
    assert L.Override(L.OVERRIDE_PARK, 0, 0).active(5)
    assert not L.Override().active(5)


def test_activity_mapping():
    assert L.lawn_mower_activity(L.STATE_IN_OPERATION, L.ACT_MOWING) == "mowing"
    assert L.lawn_mower_activity(L.STATE_RESTRICTED, L.ACT_PARKED) == "docked"
    assert L.lawn_mower_activity(L.STATE_IN_OPERATION, L.ACT_GOING_HOME) == "returning"
    assert L.lawn_mower_activity(L.STATE_PAUSED, L.ACT_MOWING) == "paused"
    assert L.lawn_mower_activity(L.STATE_ERROR, L.ACT_STOPPED_IN_GARDEN) == "error"
    assert L.lawn_mower_activity(L.STATE_STOPPED, L.ACT_CHARGING) == "docked"
    assert L.lawn_mower_activity(None, 1) is None


def test_error_text():
    names = {0: "UNEXPECTED_ERROR", 9: "TRAPPED", 58: "TEMPORARY_BATTERY_PROBLEM", 999: "SOMETHING_ODD"}
    assert L.error_text(0, names) == "Kein Fehler"
    assert L.error_text(9, names) == "Festgefahren"
    assert L.error_text(58, names) == "Vorübergehendes Akkuproblem"
    assert L.error_text(999, names) == "Something odd"
    assert L.error_text(12345, names) == "Code 12345"


def test_summary():
    assert L.schedule_summary([]) == "kein Zeitplan"
    t = L.Task(0, 3600, (False,) * 6 + (True,))
    assert L.schedule_summary([t]) == "So 00:00–01:00"
