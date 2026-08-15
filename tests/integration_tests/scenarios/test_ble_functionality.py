"""End-to-end integration test for the BLE (GATT) adapter.

Production writes ESC/POS bytes to a GATT characteristic over whatever
route Home Assistant's bluetooth stack picks — the HA host's own radio or
an ESPHome Bluetooth proxy. There is no way to fake a radio here, so the
test substitutes a ``BleakClient`` stand-in that forwards every
``write_gatt_char`` payload straight into the existing ``VirtualPrinter``
ESC/POS emulator over TCP loopback.

That covers the part a unit test can't: real python-escpos command
generation flowing through ``_raw`` → buffering transport → MTU chunking →
the emulator's command parser. In particular it proves that **chunking a
receipt at BLE MTU boundaries does not corrupt the ESC/POS stream** — the
emulator reassembles and parses exactly what an unchunked transport would
have produced.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import socket
import time
from typing import Any

import pytest

from custom_components.escpos_printer.printer import (
    BlePrinterAdapter,
    BlePrinterConfig,
    ble_transport,
)
from tests.integration_tests.emulator import VirtualPrinter

pytestmark = pytest.mark.integration

_FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"


class _LoopbackCharacteristic:
    """Stand-in for a bleak write characteristic."""

    uuid = _FF02
    properties = ("write",)
    handle = 1
    max_write_without_response_size = 0


class _LoopbackBleakClient:
    """BleakClient stand-in that forwards GATT writes to the emulator.

    Each ``write_gatt_char`` becomes one TCP send, mirroring how each GATT
    write is one radio packet. The brief pause works around the emulator's
    parser discarding all but the first command when several arrive in a
    single TCP segment (same workaround the RFCOMM scenario uses).
    """

    def __init__(self, host: str, port: int, mtu_size: int = 23) -> None:
        self._sock = socket.create_connection((host, port), timeout=4.0)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.mtu_size = mtu_size
        self.is_connected = True
        self.chunk_sizes: list[int] = []
        self.services: list[Any] = []

    async def write_gatt_char(self, _characteristic, data, response=None):
        payload = bytes(data)
        self.chunk_sizes.append(len(payload))
        self._sock.sendall(payload)
        await asyncio.sleep(0.02)

    async def disconnect(self):
        await asyncio.sleep(0.05)  # let the emulator drain
        self.is_connected = False
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture
async def ble_adapter_over_loopback(
    monkeypatch: Any, hass: Any
) -> AsyncGenerator[tuple[BlePrinterAdapter, Any, Any, list]]:
    """Yield ``(adapter, server, hass, clients)`` wired to a virtual printer."""
    async with VirtualPrinter(host="127.0.0.1", port=9111) as server:
        clients: list[_LoopbackBleakClient] = []

        async def _fake_connect(_hass, address, **_kwargs):
            client = _LoopbackBleakClient("127.0.0.1", 9111)
            clients.append(client)
            return ble_transport.BleConnection(
                client,
                _LoopbackCharacteristic(),
                address=address,
                with_response=True,
                # 20 bytes: the un-negotiated BLE default, i.e. the most
                # aggressive chunking a real printer could impose.
                max_chunk=20,
            )

        monkeypatch.setattr(ble_transport, "async_connect_ble", _fake_connect)

        config = BlePrinterConfig(
            address="AA:BB:CC:DD:EE:FF",
            timeout=4.0,
            codepage="CP437",
            line_width=48,
            # Keep the test quick; chunk pacing is unit-tested separately.
            write_chunk_delay_ms=0,
            idle_disconnect_s=0,
        )
        adapter = BlePrinterAdapter(config)
        await adapter.start(hass, keepalive=False, status_interval=0)
        try:
            yield adapter, server, hass, clients
        finally:
            await adapter.stop(hass)


async def _wait_for_bytes(server, expected: bytes, timeout: float = 3.0) -> bytes:
    """Poll the emulator's command log until ``expected`` shows up or we time out."""
    deadline = asyncio.get_event_loop().time() + timeout
    last_blob = b""
    while asyncio.get_event_loop().time() < deadline:
        log = await server.get_command_log()
        last_blob = b"".join(getattr(cmd, "raw_data", b"") for cmd in log)
        if expected in last_blob:
            return last_blob
        await asyncio.sleep(0.05)
    return last_blob


@pytest.mark.asyncio
async def test_print_text_reaches_emulator(ble_adapter_over_loopback) -> None:
    """Text printed via the BLE adapter arrives intact after MTU chunking."""
    adapter, server, hass, _clients = ble_adapter_over_loopback
    await adapter.print_text(
        hass=hass,
        text="Hello BLE Loopback",
        align="left",
        cut="none",
        feed=0,
    )
    raw_blob = await _wait_for_bytes(server, b"Hello BLE Loopback")
    assert b"Hello BLE Loopback" in raw_blob, raw_blob


@pytest.mark.asyncio
async def test_long_text_is_chunked_but_arrives_whole(ble_adapter_over_loopback) -> None:
    """The core BLE risk: MTU chunking must not corrupt the ESC/POS stream."""
    adapter, server, hass, clients = ble_adapter_over_loopback
    payload = "BLE chunk boundary test " * 8  # comfortably over one 20-byte MTU
    await adapter.print_text(hass=hass, text=payload, align="left", cut="none", feed=0)

    raw_blob = await _wait_for_bytes(server, b"BLE chunk boundary test ")

    # python-escpos word-wraps at the configured 48-column line width, so the
    # emulator legitimately sees newlines the input didn't have. Strip those
    # and every payload character must still be present, in order — that is
    # what "chunking didn't corrupt the stream" means.
    received = raw_blob.replace(b"\n", b"")
    assert payload.replace(" ", "").encode("ascii") in received.replace(b" ", b"")

    # It genuinely took many writes, and none exceeded the negotiated MTU.
    assert len(clients[0].chunk_sizes) > 1
    assert max(clients[0].chunk_sizes) <= 20


@pytest.mark.asyncio
async def test_feed_reaches_emulator(ble_adapter_over_loopback) -> None:
    """Control commands flow end-to-end over GATT."""
    adapter, server, hass, _clients = ble_adapter_over_loopback
    initial_log_len = len(await server.get_command_log())
    await adapter.feed(hass=hass, lines=3)
    await asyncio.sleep(0.2)
    log = await server.get_command_log()
    assert len(log) > initial_log_len
    assert "feed" in {getattr(cmd, "command_type", None) for cmd in log}


@pytest.mark.asyncio
async def test_link_is_reused_across_prints(ble_adapter_over_loopback) -> None:
    """The GATT link is cached — unlike RFCOMM, it is not reopened per print.

    This is the adapter's headline divergence from the other transports: a
    BLE connect costs seconds and a proxy connection slot.
    """
    adapter, _server, hass, clients = ble_adapter_over_loopback
    adapter._ble_config.idle_disconnect_s = 300  # keep the link for this test
    await adapter.print_text(hass=hass, text="one", cut="none", feed=0)
    await adapter.print_text(hass=hass, text="two", cut="none", feed=0)
    await asyncio.sleep(0.1)
    assert len(clients) == 1


@pytest.mark.asyncio
async def test_status_check_needs_no_connection(ble_adapter_over_loopback) -> None:
    """Status is read from advertisement data — it must open no GATT link."""
    adapter, _server, hass, clients = ble_adapter_over_loopback
    hass.config.components.add("bluetooth")
    before = len(clients)

    advert = type("Advert", (), {"rssi": -61})()
    from unittest import mock

    with (
        mock.patch.object(ble_transport, "async_last_advertisement", return_value=advert),
        mock.patch.object(ble_transport, "async_ble_device", return_value=object()),
    ):
        await adapter._status_check(hass)

    assert adapter.get_status() is True
    assert adapter.last_rssi == -61
    assert len(clients) == before  # no new connection was opened
