"""BLE (GATT) printer adapter implementation.

Works with any BLE ESC/POS printer Home Assistant's bluetooth integration
can reach — including one that is only in range of an **ESPHome Bluetooth
proxy** rather than the HA host's own radio. Nothing here is proxy-aware;
routing is HA's job (see ``ble_transport``).

Two deliberate divergences from the other adapters:

**The GATT link is cached, not reconnected per operation.** USB, RFCOMM and
serial all reconnect for every print because their connects are cheap. A BLE
connect is not: 1-3 s for connect plus service discovery plus MTU
negotiation, and over a proxy it also claims one of a small number of
connection slots (an ESP32 typically offers three). Reconnecting per receipt
would make back-to-back prints feel broken and would thrash the proxy's slot
allocator. Instead the link is held and reaped by an idle timer.

**Status checks are passive.** RFCOMM has to open a real link to learn
whether a printer is alive, which audibly beeps many cheap printers. BLE
printers advertise, so ``async_last_service_info`` answers the same question
with zero radio traffic from us. That is why the ≥60 s status-interval floor
the RFCOMM entries enforce does not apply here.

All connection-state mutation happens on the event loop (inside
``_async_acquire_connection`` and the idle/disconnect callbacks), never from
the executor thread, so the cached connection needs no additional locking.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from ..const import DEFAULT_CHUNK_DELAY_MS_BLE
from ..security import sanitize_log_message
from . import ble_transport
from ._escpos_ble import make_ble_escpos
from .base_adapter import EscposPrinterAdapterBase
from .config import BlePrinterConfig

_LOGGER = logging.getLogger(__name__)


class BlePrinterAdapter(EscposPrinterAdapterBase):
    """Adapter for BLE (GATT) ESC/POS printers, proxy-capable via HA."""

    # BLE links are slow and unacknowledged writes have no backpressure, so
    # a per-chunk pause is the difference between a clean receipt and
    # garbage. See const.DEFAULT_CHUNK_DELAY_MS_BLE.
    default_chunk_delay_ms = DEFAULT_CHUNK_DELAY_MS_BLE

    def __init__(self, config: BlePrinterConfig) -> None:
        super().__init__(config)
        self._ble_config = config
        self._address_redacted = sanitize_log_message(config.address)
        # python-escpos objects are per-operation; the GATT link underneath
        # them is not. `_keepalive` governs the former and stays False like
        # USB/RFCOMM/serial.
        self._keepalive = False
        self._hass: HomeAssistant | None = None
        self._connection: ble_transport.BleConnection | None = None
        self._cancel_idle: Any = None
        # Most recent advertisement RSSI, refreshed by _status_check and
        # surfaced by the diagnostic signal-strength sensor.
        self._last_rssi: int | None = None
        # GATT layout from the most recent successful connect. Kept on the
        # adapter rather than the connection so it outlives the idle
        # disconnect and is still readable in a diagnostics download.
        self._last_gatt_layout: list[dict[str, Any]] = []

    @property
    def config(self) -> BlePrinterConfig:
        """Return the BLE printer configuration."""
        return self._ble_config

    # ------------------------------------------------------------------
    # Connection cache (event loop only)
    # ------------------------------------------------------------------

    @callback
    def _cancel_idle_timer(self) -> None:
        """Stop any pending idle-disconnect callback."""
        if self._cancel_idle is not None:
            self._cancel_idle()
            self._cancel_idle = None

    @callback
    def _schedule_idle_disconnect(self, hass: HomeAssistant) -> None:
        """(Re)arm the idle timer that reaps the cached GATT link."""
        from homeassistant.helpers.event import async_call_later  # noqa: PLC0415

        self._cancel_idle_timer()
        delay = self._ble_config.idle_disconnect_s
        if delay <= 0 or self._connection is None:
            return

        async def _idle(_now: Any) -> None:
            self._cancel_idle = None
            # Never drop the link out from under an in-flight print.
            if self._lock.locked():
                self._schedule_idle_disconnect(hass)
                return
            await self._async_drop_connection()
            _LOGGER.debug(
                "BLE link to %s released after %ss idle",
                self._address_redacted,
                delay,
            )

        self._cancel_idle = async_call_later(hass, delay, _idle)

    @callback
    def _handle_disconnect(self, _client: Any) -> None:
        """Forget the cached link when the printer or proxy drops it."""
        self._connection = None
        self._cancel_idle_timer()
        _LOGGER.debug("BLE link to %s dropped by peer", self._address_redacted)

    async def _async_drop_connection(self) -> None:
        """Disconnect and forget the cached link."""
        connection = self._connection
        self._connection = None
        self._cancel_idle_timer()
        if connection is not None:
            await connection.async_disconnect()

    async def _async_acquire_connection(self) -> ble_transport.BleConnection:
        """Return a live GATT link, reusing the cached one when possible.

        Runs on the event loop, which is what makes the unguarded reads and
        writes of ``self._connection`` safe.
        """
        self._cancel_idle_timer()
        if self._connection is not None and self._connection.is_connected:
            return self._connection
        # A cached-but-dead link (peer slept, proxy rebooted) must be cleared
        # before reconnecting or we would leak the stale client.
        self._connection = None
        assert self._hass is not None  # set in start(), before any operation
        self._connection = await ble_transport.async_connect_ble(
            self._hass,
            self._ble_config.address,
            write_uuid=self._ble_config.write_uuid,
            with_response=self._ble_config.with_response,
            pair=self._ble_config.pair,
            disconnected_callback=self._handle_disconnect,
        )
        self._last_gatt_layout = self._connection.gatt_layout
        return self._connection

    # ------------------------------------------------------------------
    # Adapter interface
    # ------------------------------------------------------------------

    def _connect(self) -> Any:
        """Return a python-escpos printer bound to a live BLE link.

        Runs on an executor thread; hops to the loop once to get the
        connection, then hands back a purely synchronous byte-sink.
        """
        hass = self._hass
        if hass is None:  # pragma: no cover - defensive; start() sets it
            raise RuntimeError("BLE adapter used before start()")

        future = asyncio.run_coroutine_threadsafe(self._async_acquire_connection(), hass.loop)
        try:
            connection = future.result(ble_transport.connect_timeout(self._ble_config.timeout))
        except TimeoutError:
            future.cancel()
            _LOGGER.warning(
                "BLE connect to %s timed out",
                self._address_redacted,
            )
            raise
        except Exception as exc:
            _LOGGER.warning(
                "BLE connect to %s failed: %s",
                self._address_redacted,
                sanitize_log_message(str(exc)),
            )
            raise

        transport = ble_transport.open_ble_transport(
            hass,
            connection,
            chunk_delay_ms=self._ble_config.write_chunk_delay_ms,
        )
        return make_ble_escpos(transport, self._profile_for_constructor())

    async def _release_printer(
        self,
        hass: HomeAssistant,
        printer: Any,
        *,
        owned: bool,
        failed: bool = False,
        notify_status: bool = True,
    ) -> None:
        """Flush coalesced output, then arm the idle timer.

        Mirrors the serial adapter: the BLE transport buffers every
        ``_raw()`` micro-write, so without an explicit flush on the success
        path a write that fails at flush time — printer powered off
        mid-receipt, proxy rebooted — would be suppressed by the base
        class's best-effort close and the print reported as a success.

        A flush failure also invalidates the cached link: whatever broke the
        write has almost certainly broken the connection, and reusing it
        would fail every subsequent print until the entry reloads.
        """
        flush_exc: Exception | None = None
        if owned and not failed and printer is not None:

            def _flush() -> None:
                printer.flush()

            try:
                await hass.async_add_executor_job(_flush)
            except Exception as exc:  # surfaced after the close below
                flush_exc = exc
                failed = True
                if isinstance(exc, ble_transport.BleAuthorizationError) and not (
                    self._ble_config.pair
                ):
                    # The printer accepted the connection and only refused the
                    # data, so nothing earlier in the flow could have caught
                    # this. Name the fix rather than leaving a bare ATT error.
                    _LOGGER.error(
                        "BLE printer %s refused the write because the link is not "
                        "bonded (ATT insufficient authorization). Enable 'Pair with "
                        "printer' in this entry's options and try again.",
                        self._address_redacted,
                    )

        await super()._release_printer(
            hass, printer, owned=owned, failed=failed, notify_status=notify_status
        )

        if failed:
            await self._async_drop_connection()
        else:
            self._schedule_idle_disconnect(hass)

        if flush_exc is not None:
            raise flush_exc

    async def _status_check(self, hass: HomeAssistant) -> None:
        """Check reachability from advertisement data — no radio traffic.

        ``async_last_service_info`` reports what the bluetooth stack has
        already heard (via any adapter or proxy);
        ``async_ble_device_from_address(connectable=True)`` additionally
        confirms some scanner could actually *connect*, which a
        passive-only proxy cannot. A printer that is powered off stops
        advertising, so this goes stale exactly when it should.
        """
        address = self._ble_config.address
        if not ble_transport.bluetooth_ready(hass):
            self._last_rssi = None
            self._record_status(False, "Home Assistant's Bluetooth integration is not set up", None)
            return

        start = time.perf_counter()
        service_info = ble_transport.async_last_advertisement(hass, address)
        device = ble_transport.async_ble_device(hass, address)
        latency_ms = int((time.perf_counter() - start) * 1000)

        if device is not None:
            self._last_rssi = service_info.rssi if service_info is not None else None
            self._record_status(True, None, latency_ms)
        else:
            self._last_rssi = None
            self._record_status(
                False,
                f"No connectable Bluetooth adapter or proxy can reach {address}",
                latency_ms,
            )

    def _record_status(self, ok: bool, err: str | None, latency_ms: int | None) -> None:
        """Update status bookkeeping fields and notify listeners."""
        now = dt_util.utcnow()
        self._last_check = now
        self._last_latency_ms = latency_ms
        if ok:
            self._last_ok = now
            self._last_error_reason = None
        else:
            self._last_error = now
            self._last_error_reason = sanitize_log_message(err or "BLE printer unreachable")
        # BLE surfaces no errno anywhere in its stack; keep the field clear
        # rather than leaving a stale value from an earlier transport error.
        self._last_error_errno = None
        if self._status != ok and not ok:
            _LOGGER.warning("BLE printer %s not reachable", self._address_redacted)
        self._notify_status_change(ok)

    @property
    def last_rssi(self) -> int | None:
        """Return the most recent advertisement RSSI, if any."""
        return self._last_rssi

    def get_connection_info(self) -> str:
        """Return a human-readable connection info string."""
        return f"BLE {self._ble_config.address}"

    def get_diagnostics(self) -> dict[str, Any]:
        """Add BLE link state to the base diagnostics payload."""
        diagnostics = super().get_diagnostics()
        connection = self._connection
        diagnostics["ble"] = {
            "connected": connection is not None and connection.is_connected,
            "last_rssi": self.last_rssi,
            "idle_disconnect_s": self._ble_config.idle_disconnect_s,
            "pair": self._ble_config.pair,
            "write_uuid": connection.characteristic_uuid if connection is not None else None,
            "with_response": connection.with_response if connection is not None else None,
            "max_chunk": connection.max_chunk if connection is not None else None,
            # Survives the idle disconnect: captured at connect time so a user
            # chasing a wrong-characteristic problem can read the device's
            # actual GATT layout without the download having to wake the
            # printer. Empty until the first successful connect.
            "gatt_layout": self._last_gatt_layout,
        }
        return diagnostics

    async def start(self, hass: HomeAssistant, *, keepalive: bool, status_interval: int) -> None:
        """Start the adapter, capturing ``hass`` for the executor→loop hop.

        ``_connect()`` runs on an executor thread and needs both the device
        registry lookup and the loop handle, but the base class never stores
        ``hass``. Capture it before anything can call ``_connect()``.
        """
        self._hass = hass
        await super().start(hass, keepalive=False, status_interval=status_interval)

    async def stop(self, hass: HomeAssistant | None = None) -> None:
        """Stop the adapter and drop the cached GATT link."""
        self._cancel_idle_timer()
        await super().stop(hass)
        with contextlib.suppress(Exception):
            await self._async_drop_connection()
