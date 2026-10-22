"""Read-only test straight from the Mac (no Home Assistant, no proxy).

    .venv/bin/python tools/probe.py            # scan for mowers
    .venv/bin/python tools/probe.py <address>  # connect and read status
    .venv/bin/python tools/probe.py <address> --pin 1234

Switch the mower off and on first (pairing mode). Sends nothing that moves it.
On macOS the address is a UUID, not the MAC – that is fine here.
"""

import argparse
import asyncio
import random

from automower_ble.models import MowerModels
from automower_ble.mower import Mower
from bleak import BleakScanner

SERVICE_UUID = "98bd0001-0b0e-421a-84e5-ddbf75dc6de4"


async def scan() -> None:
    print("Suche 10 s nach Mähern …")
    found = await BleakScanner.discover(timeout=10, return_adv=True)
    hits = [(d, a) for d, a in found.values() if SERVICE_UUID in (a.service_uuids or [])]
    for d, a in hits:
        print(f"  {d.address}  {d.name}  RSSI {a.rssi}")
    if not hits:
        print("  keiner gefunden (Mäher an? in der Nähe?)")


async def probe(address: str, pin: int | None) -> None:
    device = await BleakScanner.find_device_by_address(address, timeout=15)
    if device is None:
        print("Nicht gefunden.")
        return
    mower = Mower(random.randint(1, 0xFFFFFFFF), address, pin)
    print("Verbinde …", await mower.connect(device))
    try:
        raw = await mower.command("GetModel")
        info = MowerModels.get((raw["deviceType"], raw["deviceVariant"])) if raw else None
        print("Modell:", info.model if info else raw)
        for cmd in ("GetUserMowerNameAsAsciiString", "GetSerialNumber", "GetBatteryLevel", "IsCharging",
                    "GetState", "GetActivity", "GetMode", "GetError", "GetOverride", "GetNextStartTime",
                    "GetTime", "GetNumberOfTasks", "GetAllStatistics"):
            try:
                print(f"{cmd}: {await mower.command(cmd)}")
            except Exception as err:  # noqa: BLE001
                print(f"{cmd}: Fehler {err}")
        for i in range(await mower.command("GetNumberOfTasks") or 0):
            print(f"Task {i}: {await mower.command('GetTask', taskId=i)}")
    finally:
        await mower.disconnect()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("address", nargs="?")
    p.add_argument("--pin", type=int)
    a = p.parse_args()
    asyncio.run(probe(a.address, a.pin) if a.address else scan())
