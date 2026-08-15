"""Tests for the BLE printer adapter: connection cache, idle reaping, status."""

import asyncio
from unittest.mock import patch

import pytest

from custom_components.escpos_printer.printer import (
    BlePrinterAdapter,
    BlePrinterConfig,
    ble_transport,
    create_printer_adapter,
)

_FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"


class FakeConnection:
    """Stand-in for ble_transport.BleConnection."""

    def __init__(self, *, connected=True):
        self.is_connected = connected
        self.address = "AA:BB:CC:DD:EE:FF"
        self.with_response = True
        self.max_chunk = 20
        self.characteristic_uuid = _FF02
        self.disconnect_calls = 0
        self.writes: list[bytes] = []

    async def async_write(self, data, chunk_delay_s):
        self.writes.append(bytes(data))

    async def async_disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False


@pytest.fixture
def ble_config():
    return BlePrinterConfig(
        address="AA:BB:CC:DD:EE:FF",
        timeout=4.0,
        codepage="CP437",
        profile=None,
        line_width=48,
    )


@pytest.fixture
def adapter(ble_config, hass):
    adapter = BlePrinterAdapter(ble_config)
    adapter._hass = hass
    return adapter


class TestBlePrinterConfig:
    """Config dataclass defaults."""

    def test_default_values(self):
        config = BlePrinterConfig()
        assert config.connection_type == "ble"
        assert config.address == ""
        # None means "auto-detect", which is what almost every entry uses.
        assert config.write_uuid is None
        assert config.with_response is None
        assert config.idle_disconnect_s == 30
        assert config.write_chunk_delay_ms == 20

    def test_custom_values(self):
        config = BlePrinterConfig(
            address="11:22:33:44:55:66",
            write_uuid=_FF02,
            with_response=False,
            idle_disconnect_s=0,
            write_chunk_delay_ms=100,
        )
        assert config.address == "11:22:33:44:55:66"
        assert config.write_uuid == _FF02
        assert config.with_response is False
        assert config.idle_disconnect_s == 0
        assert config.write_chunk_delay_ms == 100


class TestFactory:
    """create_printer_adapter dispatch."""

    def test_factory_returns_ble_adapter(self, ble_config):
        assert isinstance(create_printer_adapter(ble_config), BlePrinterAdapter)


class TestAdapterBasics:
    """Identity and configuration surface."""

    def test_connection_info(self, adapter):
        assert adapter.get_connection_info() == "BLE AA:BB:CC:DD:EE:FF"

    def test_config_property(self, adapter, ble_config):
        assert adapter.config is ble_config

    def test_keepalive_forced_off(self, adapter):
        """python-escpos objects are per-operation; the GATT link is not."""
        assert adapter._keepalive is False

    def test_ble_chunk_delay_default(self, adapter):
        assert adapter.default_chunk_delay_ms == 20

    async def test_start_captures_hass_for_the_executor_hop(self, ble_config, hass):
        """_connect() runs on an executor thread and needs the loop handle."""
        adapter = BlePrinterAdapter(ble_config)
        assert adapter._hass is None
        await adapter.start(hass, keepalive=True, status_interval=0)
        assert adapter._hass is hass
        # Keepalive is ignored, like USB/RFCOMM/serial.
        assert adapter._keepalive is False
        await adapter.stop(hass)

    def test_connect_before_start_raises(self, ble_config):
        adapter = BlePrinterAdapter(ble_config)
        with pytest.raises(RuntimeError, match="before start"):
            adapter._connect()


class TestConnectionCache:
    """The GATT link outlives a single print (see the adapter docstring)."""

    async def test_reuses_a_live_connection(self, adapter):
        connection = FakeConnection()
        with patch.object(ble_transport, "async_connect_ble", return_value=connection) as connect:
            first = await adapter._async_acquire_connection()
            second = await adapter._async_acquire_connection()
        assert first is second is connection
        assert connect.call_count == 1

    async def test_reconnects_when_cached_link_is_dead(self, adapter):
        """A slept printer or rebooted proxy leaves a stale client behind."""
        dead = FakeConnection(connected=False)
        fresh = FakeConnection()
        adapter._connection = dead
        with patch.object(ble_transport, "async_connect_ble", return_value=fresh) as connect:
            result = await adapter._async_acquire_connection()
        assert result is fresh
        assert connect.call_count == 1

    async def test_passes_config_overrides_to_connect(self, hass):
        config = BlePrinterConfig(
            address="AA:BB:CC:DD:EE:FF", write_uuid=_FF02, with_response=False
        )
        adapter = BlePrinterAdapter(config)
        adapter._hass = hass
        with patch.object(
            ble_transport, "async_connect_ble", return_value=FakeConnection()
        ) as connect:
            await adapter._async_acquire_connection()
        assert connect.call_args.kwargs["write_uuid"] == _FF02
        assert connect.call_args.kwargs["with_response"] is False

    async def test_disconnect_callback_clears_the_cache(self, adapter):
        adapter._connection = FakeConnection()
        adapter._handle_disconnect(object())
        assert adapter._connection is None

    async def test_drop_connection_disconnects_and_clears(self, adapter):
        connection = FakeConnection()
        adapter._connection = connection
        await adapter._async_drop_connection()
        assert connection.disconnect_calls == 1
        assert adapter._connection is None

    async def test_stop_releases_the_link(self, adapter, hass):
        connection = FakeConnection()
        adapter._connection = connection
        await adapter.stop(hass)
        assert connection.disconnect_calls == 1
        assert adapter._connection is None


class TestIdleDisconnect:
    """The timer that reaps a cached link so proxy slots aren't held forever."""

    async def test_idle_timer_disconnects_after_the_delay(self, adapter, hass):
        connection = FakeConnection()
        adapter._connection = connection
        adapter._ble_config.idle_disconnect_s = 30

        captured = {}

        def _fake_call_later(_hass, delay, action):
            captured["delay"] = delay
            captured["action"] = action
            return lambda: None

        with patch("homeassistant.helpers.event.async_call_later", side_effect=_fake_call_later):
            adapter._schedule_idle_disconnect(hass)

        assert captured["delay"] == 30
        await captured["action"](None)
        assert connection.disconnect_calls == 1
        assert adapter._connection is None

    async def test_idle_timer_defers_while_a_print_holds_the_lock(self, adapter, hass):
        """Never drop the link out from under an in-flight print."""
        connection = FakeConnection()
        adapter._connection = connection
        actions = []

        def _fake_call_later(_hass, _delay, action):
            actions.append(action)
            return lambda: None

        with patch("homeassistant.helpers.event.async_call_later", side_effect=_fake_call_later):
            adapter._schedule_idle_disconnect(hass)
            async with adapter._lock:
                await actions[0](None)
                # Deferred, not executed: the link survives and a new timer is armed.
                assert connection.disconnect_calls == 0
                assert adapter._connection is connection
                assert len(actions) == 2

    async def test_zero_delay_disables_the_cache_timer(self, adapter, hass):
        adapter._connection = FakeConnection()
        adapter._ble_config.idle_disconnect_s = 0
        with patch("homeassistant.helpers.event.async_call_later") as call_later:
            adapter._schedule_idle_disconnect(hass)
        call_later.assert_not_called()

    async def test_no_timer_without_a_connection(self, adapter, hass):
        adapter._connection = None
        with patch("homeassistant.helpers.event.async_call_later") as call_later:
            adapter._schedule_idle_disconnect(hass)
        call_later.assert_not_called()

    async def test_rearming_cancels_the_previous_timer(self, adapter, hass):
        adapter._connection = FakeConnection()
        cancels = []

        def _fake_call_later(_hass, _delay, _action):
            token = len(cancels)
            return lambda: cancels.append(token)

        with patch("homeassistant.helpers.event.async_call_later", side_effect=_fake_call_later):
            adapter._schedule_idle_disconnect(hass)
            adapter._schedule_idle_disconnect(hass)
        assert cancels == [0]


class TestReleasePrinter:
    """Flush-on-success and cache invalidation on failure."""

    async def test_flush_error_surfaces_and_drops_the_link(self, adapter, hass):
        """A failed write must not be reported as a successful print."""

        class ExplodingPrinter:
            def flush(self):
                raise OSError("printer went away")

            def close(self):
                pass

        connection = FakeConnection()
        adapter._connection = connection

        with pytest.raises(OSError, match="printer went away"):
            await adapter._release_printer(hass, ExplodingPrinter(), owned=True)

        # Whatever broke the write almost certainly broke the connection.
        assert connection.disconnect_calls == 1
        assert adapter._connection is None

    async def test_success_flushes_and_arms_the_idle_timer(self, adapter, hass):
        flushed = []

        class Printer:
            def flush(self):
                flushed.append(True)

            def close(self):
                pass

        adapter._connection = FakeConnection()
        with patch("homeassistant.helpers.event.async_call_later", return_value=lambda: None):
            await adapter._release_printer(hass, Printer(), owned=True)

        assert flushed == [True]
        # Link kept for the next print.
        assert adapter._connection is not None

    async def test_failed_operation_drops_the_link(self, adapter, hass):
        class Printer:
            def flush(self):
                pass

            def close(self):
                pass

        connection = FakeConnection()
        adapter._connection = connection
        await adapter._release_printer(hass, Printer(), owned=True, failed=True)
        assert connection.disconnect_calls == 1
        assert adapter._connection is None


class TestStatusCheck:
    """Passive reachability from advertisement data."""

    async def test_online_when_a_connectable_route_exists(self, adapter, hass):
        hass.config.components.add("bluetooth")
        advert = type("Advert", (), {"rssi": -57})()
        with (
            patch.object(ble_transport, "async_last_advertisement", return_value=advert),
            patch.object(ble_transport, "async_ble_device", return_value=object()),
        ):
            await adapter._status_check(hass)

        assert adapter.get_status() is True
        assert adapter.last_rssi == -57
        assert adapter._last_error_reason is None

    async def test_offline_when_nothing_can_reach_it(self, adapter, hass):
        hass.config.components.add("bluetooth")
        with (
            patch.object(ble_transport, "async_last_advertisement", return_value=None),
            patch.object(ble_transport, "async_ble_device", return_value=None),
        ):
            await adapter._status_check(hass)

        assert adapter.get_status() is False
        assert adapter.last_rssi is None
        assert "No connectable" in adapter._last_error_reason

    async def test_online_without_advertisement_leaves_rssi_unknown(self, adapter, hass):
        """Connectable but nothing cached: reachable, RSSI simply unknown."""
        hass.config.components.add("bluetooth")
        with (
            patch.object(ble_transport, "async_last_advertisement", return_value=None),
            patch.object(ble_transport, "async_ble_device", return_value=object()),
        ):
            await adapter._status_check(hass)

        assert adapter.get_status() is True
        assert adapter.last_rssi is None

    async def test_reports_offline_when_bluetooth_is_not_set_up(self, adapter, hass):
        await adapter._status_check(hass)
        assert adapter.get_status() is False
        assert "Bluetooth integration is not set up" in adapter._last_error_reason

    async def test_status_check_emits_no_radio_traffic(self, adapter, hass):
        """The whole point: unlike RFCOMM, this never wakes or beeps the printer."""
        hass.config.components.add("bluetooth")
        with (
            patch.object(ble_transport, "async_last_advertisement", return_value=None),
            patch.object(ble_transport, "async_ble_device", return_value=object()),
            patch.object(ble_transport, "async_connect_ble") as connect,
        ):
            await adapter._status_check(hass)
        connect.assert_not_called()


class TestDiagnostics:
    """BLE link state in the diagnostics download."""

    def test_reports_disconnected_state(self, adapter):
        diag = adapter.get_diagnostics()
        assert diag["ble"]["connected"] is False
        assert diag["ble"]["write_uuid"] is None
        assert diag["ble"]["idle_disconnect_s"] == 30

    def test_reports_negotiated_link_details(self, adapter):
        adapter._connection = FakeConnection()
        adapter._last_rssi = -60
        diag = adapter.get_diagnostics()
        assert diag["ble"] == {
            "connected": True,
            "last_rssi": -60,
            "idle_disconnect_s": 30,
            "write_uuid": _FF02,
            "with_response": True,
            "max_chunk": 20,
        }


class TestConnectHop:
    """The executor-thread to event-loop bridge in _connect()."""

    async def test_connect_returns_a_printer_bound_to_the_link(self, adapter, hass):
        """Bytes written to the python-escpos object reach the GATT link.

        Uses ``_raw`` rather than a command helper such as ``text()``: the
        unit-test fake for ``escpos.escpos.Escpos`` stubs the command layer
        out, so real ESC/POS byte generation is asserted in the integration
        scenario instead. What matters here is the transport wiring.
        """
        connection = FakeConnection()
        with patch.object(ble_transport, "async_connect_ble", return_value=connection):
            printer = await hass.async_add_executor_job(adapter._connect)

        printer._raw(b"hi")
        await hass.async_add_executor_job(printer.flush)
        assert connection.writes == [b"hi"]

    async def test_connect_failure_propagates(self, adapter, hass):
        with patch.object(
            ble_transport,
            "async_connect_ble",
            side_effect=ble_transport.BleNotFoundError("nope"),
        ):
            with pytest.raises(ble_transport.BleNotFoundError):
                await hass.async_add_executor_job(adapter._connect)

    async def test_connect_timeout_cancels_the_loop_side_work(self, adapter, hass):
        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(3600)

        with (
            patch.object(ble_transport, "async_connect_ble", side_effect=_hang),
            patch.object(ble_transport, "connect_timeout", return_value=0.05),
        ):
            with pytest.raises(TimeoutError):
                await hass.async_add_executor_job(adapter._connect)
