"""Short BLE sessions with the mower: connect, do the work, disconnect.

Holding the connection open (like the core integration) blocks a proxy slot and
the Gardena app, and one dropped link made everything unavailable. Here every
poll or command is its own session.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TypeVar

from automower_ble.mower import Mower
from automower_ble.protocol import ResponseResult
from bleak import BleakError
from bleak_retry_connector import close_stale_connections_by_address

from habluetooth import BaseHaRemoteScanner

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import LOGGER
from .frames import FIXED, build_request, parse_response, result_code

T = TypeVar("T")

CONNECT_TIMEOUT = 75
WORK_TIMEOUT = 120


class LinkError(Exception):
    """Mower not reachable or did not answer."""


class NotInRange(LinkError):
    """No recent advertisement from the mower."""


class AuthError(LinkError):
    """Wrong PIN / pairing refused."""


class MowerLink:
    """Serialises sessions with one mower."""

    def __init__(self, hass: HomeAssistant, address: str, channel_id: int, pin: int | None) -> None:
        self.hass = hass
        self.address = address
        self.channel_id = channel_id
        self.pin = pin
        self._lock = asyncio.Lock()

    def _proxy_device(self):
        """The mower as heard by a Bluetooth proxy (ESP32), strongest signal first.

        The host's own adapter is ignored: it is too far from the garden, and
        sessions through it only ran into timeouts.
        """
        seen = [
            d for d in bluetooth.async_scanner_devices_by_address(self.hass, self.address, connectable=True)
            if isinstance(d.scanner, BaseHaRemoteScanner)
        ]
        return max(seen, key=lambda d: d.advertisement.rssi).ble_device if seen else None

    def in_range(self) -> bool:
        return self._proxy_device() is not None

    async def run(self, work: Callable[[Mower], Awaitable[T]]) -> T:
        async with self._lock:
            device = self._proxy_device()
            if device is None:
                raise NotInRange("Mäher nicht in Bluetooth-Reichweite")
            mower = Mower(self.channel_id, self.address, self.pin)
            try:
                try:
                    result = await asyncio.wait_for(mower.connect(device), CONNECT_TIMEOUT)
                except (BleakError, TimeoutError, OSError) as err:
                    raise LinkError(f"Verbindung fehlgeschlagen: {str(err) or type(err).__name__}") from err
                if result in (ResponseResult.INVALID_PIN, ResponseResult.NOT_ALLOWED):
                    raise AuthError(f"Mäher lehnt ab ({result.name})")
                if result is not ResponseResult.OK:
                    raise LinkError(f"Verbindung fehlgeschlagen ({result.name})")
                try:
                    return await asyncio.wait_for(work(mower), WORK_TIMEOUT)
                except (BleakError, TimeoutError, OSError) as err:
                    raise LinkError(f"Kommunikation abgebrochen: {str(err) or type(err).__name__}") from err
            finally:
                await self._close(mower)

    async def _close(self, mower: Mower) -> None:
        mower.keep_alive_event.set()
        if task := getattr(mower, "task", None):
            task.cancel()
        if mower.client is not None and mower.is_connected():
            with suppress(Exception):
                await asyncio.wait_for(mower.disconnect(), 15)
        with suppress(Exception):
            await close_stale_connections_by_address(self.address)
        LOGGER.debug("Session mit %s beendet", self.address)


async def ask(mower: Mower, command: str, **kwargs):
    """Send one command and parse the answer (None if the mower refuses it).

    A missing answer with the link gone raises LinkError.
    """
    spec = FIXED.get(command) or (await mower.get_protocol())[command]
    response = await mower._request_response(build_request(mower.channel_id, spec, **kwargs))  # noqa: SLF001
    if response is None or len(response) < 19:
        if not mower.is_connected():
            raise LinkError(f"Keine Antwort auf {command}")
        return None
    if code := result_code(response):
        quiet = command.startswith("Get") or command == "StartTrigger"
        LOGGER.log(logging.DEBUG if quiet else logging.WARNING, "%s: Mäher antwortet mit Ergebnis %s", command, code)
        return None
    return parse_response(response, spec.get("responseType"))
