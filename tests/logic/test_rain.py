from datetime import datetime, timedelta, timezone

import logic as L

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
LEAD, DRY, MIN = timedelta(minutes=30), timedelta(minutes=60), timedelta(minutes=45)


def fc(*rainy_hours, mm=1.0):
    out = []
    for h in range(0, 12):
        out.append({"datetime": (T0 + timedelta(hours=h)).isoformat(), "condition": "rainy" if h in rainy_hours else "sunny",
                    "precipitation": mm if h in rainy_hours else 0.0})
    return out


def state(now, forecast, raining=False, last_end=None):
    return L.rain_state(now, raining, L.rain_spells(forecast, datetime.fromisoformat), last_end, LEAD, DRY)


def test_clear_day():
    s = state(T0, fc())
    assert s.can_start(T0, MIN) and not s.must_go_home(T0) and s.status(T0, LEAD) == "clear"


def test_rain_at_three_go_home_by_half_past_two():
    s = state(T0, fc(3, 4))
    assert s.next_rain == T0 + timedelta(hours=3) and s.rain_end == T0 + timedelta(hours=5)
    assert s.leave_by == T0 + timedelta(hours=2, minutes=30)
    assert not s.must_go_home(T0 + timedelta(hours=2))
    assert s.must_go_home(T0 + timedelta(hours=2, minutes=30))
    # not worth starting 20 min before leave_by
    assert not s.can_start(T0 + timedelta(hours=2, minutes=10), MIN)


def test_raining_now_blocks_until_dry():
    s = state(T0 + timedelta(minutes=10), fc(0, 1))
    assert s.raining and s.blocked_until == T0 + timedelta(hours=3)
    assert s.status(T0, LEAD) == "raining"


def test_wet_grass_after_rain():
    now = T0 + timedelta(hours=2, minutes=20)
    s = state(now, fc(), last_end=T0 + timedelta(hours=2))
    assert s.blocked(now) and s.status(now, LEAD) == "drying"
    assert s.can_start(T0 + timedelta(hours=3), MIN)


def test_drizzle_below_threshold_ignored():
    f = fc()
    f[2]["precipitation"] = 0.1
    assert state(T0, f).next_rain is None


def test_actual_rain_without_forecast():
    s = state(T0, fc(), raining=True)
    assert s.raining and s.must_go_home(T0)


def test_frost_hours_block_like_rain():
    f = fc()
    for e in f[:3]:
        e["temperature"] = 2.0
    for e in f[3:]:
        e["temperature"] = 9.0
    spells = L.rain_spells(f, datetime.fromisoformat, min_temp=5)
    s = L.rain_state(T0, False, spells, None, LEAD, DRY)
    assert s.raining and s.blocked_until == T0 + timedelta(hours=4)
    assert not L.rain_spells(f, datetime.fromisoformat, min_temp=None)


def test_snow_is_cold():
    assert L.is_cold(10, "snowy", 5) and L.is_cold(4.9, "sunny", 5) and not L.is_cold(4.9, "sunny", None)
