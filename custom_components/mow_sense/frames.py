"""Own request/response framing on top of automower_ble's protocol table.

automower-ble 0.2.9 (latest on PyPI) has wrong layouts for AddTask, GetTask and
GetOverride (fixed upstream, unreleased) and cannot encode bool parameters. Real
answers from a SILENO minimo carry extra trailing bytes, which the library
rejects. So we build and parse frames here and only borrow its transport.
"""

from __future__ import annotations

from automower_ble.helpers import crc

# Layouts from upstream main (alistair23/AutoMower-BLE, 2026-09), checked against
# real frames from a SILENO minimo 250.
FIXED: dict[str, dict] = {
    "AddTask": {
        "major": 4690,
        "minor": 7,
        "requestType": {
            "start": "uint32",
            "duration": "uint32",
            "useOnMonday": "bool",
            "useOnTuesday": "bool",
            "useOnWednesday": "bool",
            "useOnThursday": "bool",
            "useOnFriday": "bool",
            "useOnSaturday": "bool",
            "useOnSunday": "bool",
            "unknown": "uint16",
        },
    },
    "GetTask": {
        "major": 4690,
        "minor": 5,
        "requestType": {"taskId": "uint32"},
        "responseType": {
            "start": "uint32",
            "duration": "uint32",
            "useOnMonday": "bool",
            "useOnTuesday": "bool",
            "useOnWednesday": "bool",
            "useOnThursday": "bool",
            "useOnFriday": "bool",
            "useOnSaturday": "bool",
            "useOnSunday": "bool",
        },
    },
    "GetOverride": {
        "major": 4658,
        "minor": 2,
        "responseType": {"action": "uint8", "startTime": "tUnixTime", "duration": "uint32"},
    },
}

SIZES = {"uint8": 1, "bool": 1, "uint16": 2, "sint16": 2, "uint32": 4, "tUnixTime": 4}


def build_request(channel_id: int, spec: dict, **kwargs: int) -> bytearray:
    payload = bytearray()
    for name, kind in (spec.get("requestType") or {}).items():
        if name not in kwargs:
            raise ValueError(f"missing parameter {name}")
        payload += int(kwargs[name]).to_bytes(SIZES[kind], "little", signed=kind.startswith("s"))
    data = bytearray(18)
    data[0] = 0x02
    data[1] = 0xFD
    data[4:8] = channel_id.to_bytes(4, "little")
    data[8] = 0x01
    data[10] = 0x00  # request
    data[11] = 0xAF
    data[12:14] = spec["major"].to_bytes(2, "little")
    data[14] = spec["minor"]
    data[16:18] = len(payload).to_bytes(2, "little")
    data += payload
    data[2] = len(data) - 2
    data[9] = crc(data, 1, 8)
    data.append(crc(data, 1, len(data) - 1))
    data.append(0x03)
    return data


def result_code(response: bytes) -> int:
    return response[16]


def parse_response(response: bytes, response_type) -> int | str | dict | None:
    """Lenient: missing trailing fields stay out, extra bytes are ignored."""
    if response_type is None:
        return None
    length = response[17] | response[18] << 8
    data = bytes(response[19 : 19 + length])
    fields = response_type if isinstance(response_type, dict) else {"response": response_type}
    out: dict[str, int | str] = {}
    pos = 0
    for name, kind in fields.items():
        if kind in ("ascii", "utf16"):
            raw = data[pos:]
            out[name] = (raw.decode("utf-16-le") if kind == "utf16" else raw.decode("ascii", "replace")).rstrip("\x00")
            pos = len(data)
        elif kind == "remaining_uint":
            out[name] = int.from_bytes(data[pos:], "little")
            pos = len(data)
        else:
            size = SIZES[kind]
            if pos + size > len(data):
                break
            out[name] = int.from_bytes(data[pos : pos + size], "little", signed=kind.startswith("s"))
            pos += size
    if not isinstance(response_type, dict):
        return out.get("response")
    return out
