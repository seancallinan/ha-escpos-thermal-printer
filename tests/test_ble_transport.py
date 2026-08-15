"""Tests for the BLE transport: coalescing, chunking, and the loop bridge."""

import asyncio
from unittest.mock import patch

import pytest

from custom_components.escpos_printer.printer import ble_transport
from custom_components.escpos_printer.printer.ble_gatt import BleWriteCharacteristicError

_FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"


class FakeCharacteristic:
    def __init__(self, uuid=_FF02, properties=("write",), max_write_without_response_size=0):
        self.uuid = uuid
        self.properties = list(properties)
        self.handle = 1
        self.max_write_without_response_size = max_write_without_response_size


class FakeBleakClient:
    """Records writes so tests can assert on the exact wire bytes."""

    def __init__(self, *, mtu_size=23, fail_on_write=None):
        self.mtu_size = mtu_size
        self.is_connected = True
        self.writes: list[tuple[bytes, bool]] = []
        self.disconnect_calls = 0
        self._fail_on_write = fail_on_write

    async def write_gatt_char(self, characteristic, data, response=None):
        if self._fail_on_write is not None and len(self.writes) >= self._fail_on_write:
            raise OSError("printer went away")
        self.writes.append((bytes(data), bool(response)))

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False

    @property
    def written(self) -> bytes:
        return b"".join(chunk for chunk, _ in self.writes)


def _connection(client=None, *, max_chunk=20, with_response=True):
    return ble_transport.BleConnection(
        client or FakeBleakClient(),
        FakeCharacteristic(),
        address="AA:BB:CC:DD:EE:FF",
        with_response=with_response,
        max_chunk=max_chunk,
    )


class TestBleConnection:
    """The loop-side connection object."""

    async def test_write_splits_at_max_chunk(self):
        client = FakeBleakClient()
        connection = _connection(client, max_chunk=4)
        await connection.async_write(b"abcdefghij", 0.0)
        assert [chunk for chunk, _ in client.writes] == [b"abcd", b"efgh", b"ij"]
        assert client.written == b"abcdefghij"

    async def test_write_passes_response_flag_through(self):
        client = FakeBleakClient()
        await _connection(client, with_response=False).async_write(b"abc", 0.0)
        assert client.writes == [(b"abc", False)]

    async def test_write_sleeps_between_chunks_but_not_after_the_last(self):
        """A trailing sleep would add a visible pause to every print."""
        client = FakeBleakClient()
        connection = _connection(client, max_chunk=2)
        with patch.object(ble_transport.asyncio, "sleep") as sleep:
            await connection.async_write(b"abcdef", 0.05)
        assert sleep.await_count == 2  # three chunks, two gaps
        sleep.assert_awaited_with(0.05)

    async def test_no_sleep_when_delay_is_zero(self):
        client = FakeBleakClient()
        with patch.object(ble_transport.asyncio, "sleep") as sleep:
            await _connection(client, max_chunk=2).async_write(b"abcd", 0.0)
        assert sleep.await_count == 0

    async def test_empty_payload_writes_nothing(self):
        client = FakeBleakClient()
        await _connection(client).async_write(b"", 0.0)
        assert client.writes == []

    async def test_disconnect_suppresses_errors(self):
        class Exploding(FakeBleakClient):
            async def disconnect(self):
                raise OSError("already gone")

        await _connection(Exploding()).async_disconnect()  # must not raise

    def test_is_connected_reflects_client(self):
        client = FakeBleakClient()
        connection = _connection(client)
        assert connection.is_connected is True
        client.is_connected = False
        assert connection.is_connected is False

    def test_characteristic_uuid_exposed_for_diagnostics(self):
        assert _connection().characteristic_uuid == _FF02


class TestBluetoothReadiness:
    """The after_dependencies guard."""

    def test_ready_when_component_loaded(self, hass):
        hass.config.components.add("bluetooth")
        assert ble_transport.bluetooth_ready(hass) is True

    def test_not_ready_when_component_absent(self, hass):
        assert "bluetooth" not in hass.config.components
        assert ble_transport.bluetooth_ready(hass) is False

    async def test_connect_raises_when_bluetooth_not_set_up(self, hass):
        """A network-printer user has no bluetooth integration; say so clearly."""
        with pytest.raises(ble_transport.BluetoothUnavailableError, match="not set up"):
            await ble_transport.async_connect_ble(hass, "AA:BB:CC:DD:EE:FF")


class TestAsyncConnectBle:
    """Device lookup, connect, and characteristic resolution."""

    async def test_raises_not_found_when_no_connectable_route(self, hass):
        hass.config.components.add("bluetooth")
        with patch.object(ble_transport, "async_ble_device", return_value=None):
            with pytest.raises(ble_transport.BleNotFoundError, match="reach"):
                await ble_transport.async_connect_ble(hass, "AA:BB:CC:DD:EE:FF")

    async def test_looks_the_device_up_by_address(self, hass):
        hass.config.components.add("bluetooth")
        with patch.object(ble_transport, "async_ble_device", return_value=None) as lookup:
            with pytest.raises(ble_transport.BleNotFoundError):
                await ble_transport.async_connect_ble(hass, "AA:BB:CC:DD:EE:FF")
        assert lookup.call_args.args[1] == "AA:BB:CC:DD:EE:FF"

    async def test_disconnects_when_no_write_characteristic(self, hass):
        """Never leave a scarce proxy connection slot held after a failure."""
        hass.config.components.add("bluetooth")
        client = FakeBleakClient()
        with (
            patch.object(ble_transport, "async_ble_device", return_value=object()),
            patch("bleak_retry_connector.establish_connection", return_value=client),
            patch.object(
                ble_transport,
                "select_write_characteristic",
                side_effect=BleWriteCharacteristicError("nothing writable"),
            ),
        ):
            with pytest.raises(BleWriteCharacteristicError):
                await ble_transport.async_connect_ble(hass, "AA:BB:CC:DD:EE:FF")
        assert client.disconnect_calls == 1

    async def test_resolves_response_mode_and_chunk_size(self, hass):
        hass.config.components.add("bluetooth")
        client = FakeBleakClient(mtu_size=247)
        characteristic = FakeCharacteristic(properties=("write",))
        with (
            patch.object(ble_transport, "async_ble_device", return_value=object()),
            patch("bleak_retry_connector.establish_connection", return_value=client),
            patch.object(ble_transport, "select_write_characteristic", return_value=characteristic),
        ):
            connection = await ble_transport.async_connect_ble(hass, "AA:BB:CC:DD:EE:FF")

        assert connection.with_response is True
        assert connection.max_chunk == 244

    async def test_explicit_with_response_override_beats_detection(self, hass):
        hass.config.components.add("bluetooth")
        client = FakeBleakClient(mtu_size=247)
        characteristic = FakeCharacteristic(
            properties=("write",), max_write_without_response_size=100
        )
        with (
            patch.object(ble_transport, "async_ble_device", return_value=object()),
            patch("bleak_retry_connector.establish_connection", return_value=client),
            patch.object(ble_transport, "select_write_characteristic", return_value=characteristic),
        ):
            connection = await ble_transport.async_connect_ble(
                hass, "AA:BB:CC:DD:EE:FF", with_response=False
            )

        assert connection.with_response is False
        # Unacknowledged writes use the characteristic's reported size.
        assert connection.max_chunk == 100


class TestBluetoothApiSeam:
    """Every HA bluetooth read must ask for *connectable* devices only.

    This is the contract the whole proxy story rests on: a passive-only
    scanner (an ESPHome proxy without ``active: true``) reports printers it
    can never open a GATT link to. Acting on those sightings would offer
    devices in the picker that fail at the probe, and would report a printer
    as reachable when it is not.

    ``homeassistant.components.bluetooth`` can't be imported here — it pulls
    in HA's usb component, whose ``aiousbwatcher`` dependency the test
    harness doesn't ship — so a stub module is injected instead.
    """

    @pytest.fixture
    def fake_bluetooth(self, monkeypatch):
        import sys
        import types

        from homeassistant import components

        module = types.ModuleType("homeassistant.components.bluetooth")
        calls: dict[str, dict] = {}

        def _record(name, result):
            def _fn(*args, **kwargs):
                calls[name] = {"args": args, "kwargs": kwargs}
                return result

            return _fn

        module.async_ble_device_from_address = _record("device", "a-device")
        module.async_last_service_info = _record("last", "an-advert")
        module.async_discovered_service_info = _record("discovered", ["one", "two"])

        monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", module)
        monkeypatch.setattr(components, "bluetooth", module, raising=False)
        return calls

    def test_device_lookup_is_connectable_only(self, hass, fake_bluetooth):
        assert ble_transport.async_ble_device(hass, "AA:BB:CC:DD:EE:FF") == "a-device"
        assert fake_bluetooth["device"]["kwargs"] == {"connectable": True}
        assert fake_bluetooth["device"]["args"] == (hass, "AA:BB:CC:DD:EE:FF")

    def test_last_advertisement_is_connectable_only(self, hass, fake_bluetooth):
        assert ble_transport.async_last_advertisement(hass, "AA:BB:CC:DD:EE:FF") == "an-advert"
        assert fake_bluetooth["last"]["kwargs"] == {"connectable": True}

    def test_discovery_is_connectable_only(self, hass, fake_bluetooth):
        assert ble_transport.async_discovered_devices(hass) == ["one", "two"]
        assert fake_bluetooth["discovered"]["kwargs"] == {"connectable": True}


class TestBleTransportBuffering:
    """The sync byte-sink handed to python-escpos."""

    async def test_writes_are_coalesced_into_one_flush(self, hass):
        """python-escpos emits a write per attribute; one loop hop must cover all."""
        client = FakeBleakClient()
        transport = ble_transport.open_ble_transport(hass, _connection(client, max_chunk=1024))

        transport.write(b"ESC")
        transport.write(b"@")
        transport.write(b"hello")
        assert client.writes == []  # nothing on the wire yet

        await hass.async_add_executor_job(transport.flush)
        assert client.written == b"ESC@hello"
        assert len(client.writes) == 1

    async def test_flush_chunks_by_connection_max_chunk(self, hass):
        client = FakeBleakClient()
        transport = ble_transport.open_ble_transport(hass, _connection(client, max_chunk=4))
        transport.write(b"abcdefghij")
        await hass.async_add_executor_job(transport.flush)
        assert [chunk for chunk, _ in client.writes] == [b"abcd", b"efgh", b"ij"]

    async def test_empty_write_is_ignored(self, hass):
        client = FakeBleakClient()
        transport = ble_transport.open_ble_transport(hass, _connection(client))
        transport.write(b"")
        await hass.async_add_executor_job(transport.flush)
        assert client.writes == []

    async def test_flush_is_idempotent(self, hass):
        """close() flushes too; a second flush must not resend the receipt."""
        client = FakeBleakClient()
        transport = ble_transport.open_ble_transport(hass, _connection(client, max_chunk=1024))
        transport.write(b"abc")
        await hass.async_add_executor_job(transport.flush)
        await hass.async_add_executor_job(transport.flush)
        assert client.written == b"abc"

    async def test_flush_propagates_write_errors(self, hass):
        """A failed write must surface, not be reported as a successful print."""
        client = FakeBleakClient(fail_on_write=0)
        transport = ble_transport.open_ble_transport(hass, _connection(client))
        transport.write(b"abc")
        with pytest.raises(OSError, match="printer went away"):
            await hass.async_add_executor_job(transport.flush)

    async def test_close_suppresses_write_errors(self, hass):
        """close() runs on cleanup paths and must never raise."""
        client = FakeBleakClient(fail_on_write=0)
        transport = ble_transport.open_ble_transport(hass, _connection(client))
        transport.write(b"abc")
        await hass.async_add_executor_job(transport.close)  # must not raise

    async def test_close_flushes_pending_bytes(self, hass):
        client = FakeBleakClient()
        transport = ble_transport.open_ble_transport(hass, _connection(client, max_chunk=1024))
        transport.write(b"abc")
        await hass.async_add_executor_job(transport.close)
        assert client.written == b"abc"

    async def test_close_does_not_disconnect_the_link(self, hass):
        """The adapter's idle timer owns the connection, not the transport."""
        client = FakeBleakClient()
        transport = ble_transport.open_ble_transport(hass, _connection(client))
        transport.write(b"abc")
        await hass.async_add_executor_job(transport.close)
        assert client.disconnect_calls == 0

    async def test_chunk_delay_is_applied_on_flush(self, hass):
        client = FakeBleakClient()
        transport = ble_transport.open_ble_transport(
            hass, _connection(client, max_chunk=2), chunk_delay_ms=50
        )
        transport.write(b"abcd")
        with patch.object(ble_transport.asyncio, "sleep") as sleep:
            await hass.async_add_executor_job(transport.flush)
        sleep.assert_awaited_once_with(0.05)


class TestFlushTimeout:
    """The executor-side budget scales with payload size."""

    def test_budget_grows_with_chunk_count(self, hass):
        transport = ble_transport.open_ble_transport(
            hass, _connection(max_chunk=20), chunk_delay_ms=100
        )
        small = transport._flush_timeout(20)
        large = transport._flush_timeout(20_000)
        assert large > small

    async def test_timeout_cancels_the_orphaned_write(self, hass):
        """Give up on the loop-side write instead of letting it keep pushing."""
        started = asyncio.Event()

        class Hanging(FakeBleakClient):
            async def write_gatt_char(self, characteristic, data, response=None):
                started.set()
                await asyncio.sleep(3600)

        transport = ble_transport.open_ble_transport(hass, _connection(Hanging()))
        transport.write(b"abc")
        with patch.object(transport, "_flush_timeout", return_value=0.05):
            with pytest.raises(TimeoutError):
                await hass.async_add_executor_job(transport.flush)


class TestConnectTimeout:
    """Executor-side budget for the connect hop."""

    def test_leaves_room_for_internal_retries(self):
        """bleak-retry-connector retries with backoff; don't abandon it early."""
        assert ble_transport.connect_timeout(4.0) > 4.0

    def test_scales_with_configured_timeout(self):
        assert ble_transport.connect_timeout(10.0) > ble_transport.connect_timeout(4.0)
