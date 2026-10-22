"""Frames captured from a real SILENO minimo 250 (2026-10-22, channel f3121674)."""

import frames as F

CH = int.from_bytes(bytes.fromhex("f3121674"), "little")
PROTO = {
    "GetBatteryLevel": {"major": 4106, "minor": 20, "responseType": "uint8"},
    "GetUserMowerNameAsAsciiString": {"major": 4698, "minor": 5, "responseType": "ascii"},
    "GetAllStatistics": {"major": 4726, "minor": 0, "responseType": {
        "totalRunningTime": "uint32", "totalCuttingTime": "uint32", "totalChargingTime": "uint32",
        "totalSearchingTime": "uint32", "numberOfCollisions": "uint32", "numberOfChargingCycles": "uint32",
        "cuttingBladeUsageTime": "uint32"}},
}


def r(hexstr):
    return bytes.fromhex(hexstr)


def test_request_matches_real_frame():
    assert F.build_request(CH, PROTO["GetBatteryLevel"])[2] == 20 - 4
    assert F.build_request(CH, PROTO["GetBatteryLevel"]).hex() == "02fd1000f3121674013f00af0a1014000000bf03"


def test_simple_answers():
    assert F.parse_response(r("02fd1200f3121674014501af0a1014000001003abf03"), "uint8") == 58
    name = r("02fd2600f312167401a501af5a12050000150053494c454e4f206d696e696d6f20323530000000004703")
    assert F.parse_response(name, "ascii") == "SILENO minimo 250"


def test_task_with_extra_bytes():
    resp = r("02fd2400f312167401df01af52120500001300d0b60000383100000101010101010100000000c303")
    t = F.parse_response(resp, F.FIXED["GetTask"]["responseType"])
    assert t["start"] == 13 * 3600 and t["duration"] == 3.5 * 3600
    assert all(t[k] == 1 for k in t if k.startswith("useOn"))


def test_override_with_reserved_bytes():
    resp = r("02fd1e00f3121674014001af32120200000d00000000000000000000000000008e03")
    assert F.parse_response(resp, F.FIXED["GetOverride"]["responseType"]) == {"action": 0, "startTime": 0, "duration": 0}


def test_statistics():
    resp = r("02fd2d00f3121674011301af76120000001c0074d5370073c5330060df10004e9503005a5e0000ea01000073c533008b03")
    s = F.parse_response(resp, PROTO["GetAllStatistics"]["responseType"])
    assert s["numberOfChargingCycles"] == 490 and round(s["totalCuttingTime"] / 3600, 1) == 942.5


def test_addtask_layout():
    req = F.build_request(CH, F.FIXED["AddTask"], start=46800, duration=12600, useOnMonday=1, useOnTuesday=0,
                          useOnWednesday=0, useOnThursday=0, useOnFriday=1, useOnSaturday=0, useOnSunday=1, unknown=0)
    payload = req[18:-2]
    assert payload.hex() == "d0b60000" + "38310000" + "01000000010001" + "0000"
    assert req[16] == 17 and req[2] == len(req) - 4  # like the real frames


def test_missing_trailing_field_is_left_out():
    resp = bytearray(r("02fd1200f3121674014501af0a1014000001003abf03"))
    assert F.parse_response(resp, {"a": "uint8", "b": "uint32"}) == {"a": 58}
