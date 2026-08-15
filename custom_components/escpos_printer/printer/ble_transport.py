"""BLE GATT transport seam for Bluetooth Low Energy printers.

Unlike every other transport in this integration, BLE has no synchronous
API: bleak is async and its client objects are bound to the event loop that
created them. The rest of the printer stack is synchronous and runs on
executor threads (python-escpos calls ``_raw()`` dozens of times per
receipt). This module is where those two worlds meet.

Structure:

* :class:`BleConnection` — async, lives on the event loop, owns the
  ``BleakClient`` and the resolved write characteristic. The adapter caches
  one of these across prints (see ``ble_adapter`` for why).
* :class:`_BleTransport` — sync byte-sink handed to python-escpos. Buffers
  every ``_raw()`` write and, on ``flush()``, hops to the event loop once
  via ``run_coroutine_threadsafe`` to push the whole receipt out in
  MTU-sized chunks.
* :func:`async_connect_ble` / :func:`open_ble_transport` — the swappable
  seams, mirroring ``open_rfcomm_transport`` and ``open_serial_transport``.
  Tests monkeypatch these.

**Connections route through Home Assistant's bluetooth integration**, which
is what makes ESPHome (and Shelly, and any other) Bluetooth proxies work:
``async_ble_device_from_address(..., connectable=True)`` returns a device
handle backed by whichever adapter or proxy currently has the best path to
it. Nothing in this module is proxy-aware — a proxied printer and a
locally-radioed one take identical code paths.

Deadlock note: ``run_coroutine_threadsafe(...).result()`` is called from an
executor thread while the event loop is free (the loop is awaiting the
``async_add_executor_job`` that got us here, not blocking on this thread),
so the hop cannot deadlock. It would if the loop ever waited on the
executor job synchronously — it does not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from ..security import sanitize_log_message
from .ble_gatt import (
    BleWriteCharacteristicError,
    describe_services,
    max_write_size,
    prefers_response,
    select_write_characteristic,
)
from .transport_utils import iter_chunks

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

# Extra head-room added to the user's timeout when waiting on the event-loop
# hop for a connect. bleak-retry-connector makes several attempts internally
# with its own backoff, so the wall-clock connect budget is much larger than
# a single-attempt timeout; without the head-room the executor side would
# give up while the loop side is still usefully retrying.
_CONNECT_TIMEOUT_MULTIPLIER = 4
_CONNECT_TIMEOUT_HEADROOM_S = 10.0


def connect_timeout(timeout: float) -> float:
    """Return the executor-side budget for waiting on a connect hop.

    Deliberately much larger than ``timeout`` itself: bleak-retry-connector
    retries internally with its own backoff, and the executor side must not
    abandon the hop while the loop side is still usefully retrying.
    """
    return timeout * _CONNECT_TIMEOUT_MULTIPLIER + _CONNECT_TIMEOUT_HEADROOM_S


# Head-room for the flush hop. A full-width image over BLE is genuinely slow
# (hundreds of chunks, each with an inter-chunk delay), so the flush budget
# is derived from the payload size rather than the connect timeout.
_FLUSH_BASE_TIMEOUT_S = 30.0


class BleNotFoundError(Exception):
    """Raised when HA's bluetooth stack has no connectable route to the device.

    Either the printer is out of range of every adapter and proxy, or the
    only scanners that can see it are non-connectable (a passive-only proxy,
    or ESPHome without ``active: true``).
    """


class BleAuthorizationError(Exception):
    """Raised when the printer rejects writes because the link isn't bonded.

    ATT error 0x08 (Insufficient Authorization) / 0x05 (Insufficient
    Authentication) mean the connection succeeded and the characteristic
    resolved, but the peripheral will not accept data over an unbonded
    link. The fix is the ``pair`` option, not a different characteristic —
    so this is surfaced distinctly rather than folded into a generic
    "connect failed".
    """


# Substrings that identify an ATT authorization/authentication rejection.
# The bleak backends and the ESPHome proxy each phrase it differently and
# none expose a stable error code through the exception type, so string
# matching is the only portable signal available.
_AUTH_ERROR_MARKERS = (
    "insufficient authorization",
    "insufficient authentication",
    "insufficient encryption",
    "not paired",
    "att error: 0x05",
    "att error: 0x08",
    "att error: 0x0f",
)


def is_authorization_error(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like an ATT authorization rejection."""
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


class BluetoothUnavailableError(Exception):
    """Raised when HA's bluetooth integration isn't set up at all.

    The manifest declares ``after_dependencies`` rather than
    ``dependencies``: a user with a network or USB printer must not be
    forced to have the bluetooth integration, so it may legitimately be
    absent. Only the BLE paths care, and they say so clearly instead of
    raising an opaque KeyError from inside the bluetooth component.
    """


def bluetooth_ready(hass: HomeAssistant) -> bool:
    """Return True when HA's bluetooth integration is set up in this instance."""
    return "bluetooth" in hass.config.components


def _require_bluetooth(hass: HomeAssistant) -> None:
    """Raise :class:`BluetoothUnavailableError` if bluetooth isn't set up."""
    if not bluetooth_ready(hass):
        raise BluetoothUnavailableError(
            "Home Assistant's Bluetooth integration is not set up. Add a "
            "Bluetooth adapter or a Bluetooth proxy (e.g. ESPHome with "
            "'bluetooth_proxy: active: true') before configuring a BLE printer."
        )


# --------------------------------------------------------------------------
# Home Assistant bluetooth API seam
#
# Every read of HA's bluetooth state goes through these three functions, for
# two reasons. First, ``homeassistant.components.bluetooth`` transitively
# imports ``homeassistant.components.usb``, whose ``aiousbwatcher``
# dependency the test harness does not ship — so importing it at module
# scope would break collection. Second, it gives tests one patch point per
# operation instead of reaching into HA internals, mirroring how
# ``open_rfcomm_transport`` and ``open_serial_transport`` act as seams for
# the other transports.
#
# ``connectable=True`` is pinned in all three: a passive-only scanner (an
# ESPHome proxy without ``active: true``) can report a printer it could
# never open a GATT link to, and acting on those sightings produces
# confusing "discovered but won't print" failures.
# --------------------------------------------------------------------------


def async_ble_device(hass: HomeAssistant, address: str) -> Any | None:
    """Return a connectable BLEDevice for ``address``, or ``None``."""
    from homeassistant.components import bluetooth  # noqa: PLC0415

    return bluetooth.async_ble_device_from_address(hass, address, connectable=True)


def async_last_advertisement(hass: HomeAssistant, address: str) -> Any | None:
    """Return the last advertisement heard from ``address``, or ``None``."""
    from homeassistant.components import bluetooth  # noqa: PLC0415

    return bluetooth.async_last_service_info(hass, address, connectable=True)


def async_discovered_devices(hass: HomeAssistant) -> list[Any]:
    """Return advertisements for every connectable device HA can see."""
    from homeassistant.components import bluetooth  # noqa: PLC0415

    return list(bluetooth.async_discovered_service_info(hass, connectable=True))


class BleConnection:
    """A live BLE GATT link to a printer, owned by the event loop.

    Instances are created by :func:`async_connect_ble` and cached by the
    adapter. All methods must be awaited on the event loop; the synchronous
    side goes through :class:`_BleTransport`.
    """

    def __init__(
        self,
        client: Any,
        characteristic: Any,
        *,
        address: str,
        with_response: bool,
        max_chunk: int,
        gatt_layout: list[dict[str, Any]] | None = None,
    ) -> None:
        self._client = client
        self._characteristic = characteristic
        self.address = address
        self.with_response = with_response
        self.max_chunk = max_chunk
        # Captured at connect time so diagnostics can show the device's GATT
        # layout without opening a link (the adapter drops the connection
        # after each idle period, and a diagnostics download must never wake
        # the printer).
        self.gatt_layout = gatt_layout or []

    @property
    def client(self) -> Any:
        """Return the underlying bleak client (diagnostics/service dump)."""
        return self._client

    @property
    def characteristic_uuid(self) -> str:
        """Return the UUID ESC/POS bytes are written to."""
        return str(self._characteristic.uuid)

    @property
    def is_connected(self) -> bool:
        """Return True while the GATT link is up."""
        return bool(getattr(self._client, "is_connected", False))

    async def async_write(self, data: bytes, chunk_delay_s: float) -> None:
        """Write ``data`` to the printer in MTU-sized chunks.

        A write rejected for authorization is re-raised as
        :class:`BleAuthorizationError` so the adapter can tell the user to
        enable pairing instead of reporting an opaque GATT failure.

        Deliberately no retry: a receipt is not idempotent, and a write that
        failed partway through has already put ink on paper. Retrying risks a
        double print, which the project treats as worse than a failed one
        (see ROADMAP: "Retry only on positive evidence the job did not go").
        """
        for chunk, is_last in iter_chunks(data, self.max_chunk):
            try:
                await self._client.write_gatt_char(
                    self._characteristic, chunk, response=self.with_response
                )
            except Exception as exc:
                if is_authorization_error(exc):
                    raise BleAuthorizationError(str(exc)) from exc
                raise
            if chunk_delay_s > 0 and not is_last:
                await asyncio.sleep(chunk_delay_s)

    async def async_disconnect(self) -> None:
        """Drop the GATT link, suppressing teardown errors."""
        with contextlib.suppress(Exception):
            await self._client.disconnect()


async def async_connect_ble(
    hass: HomeAssistant,
    address: str,
    *,
    write_uuid: str | None = None,
    with_response: bool | None = None,
    pair: bool = False,
    disconnected_callback: Callable[[Any], None] | None = None,
) -> BleConnection:
    """Establish a GATT link to ``address`` and resolve its write channel.

    Routes through HA's bluetooth integration, so an ESPHome proxy is used
    transparently when it has the better path to the printer.

    ``pair`` bonds the link after connecting. Some printers (MTP-II among
    them) reject writes on an unbonded link with ATT 0x08, which surfaces as
    :class:`BleAuthorizationError` on the first print rather than at connect
    time — the peripheral accepts the connection and only refuses the data.

    Raises :class:`BluetoothUnavailableError` when the bluetooth integration
    isn't set up, :class:`BleNotFoundError` when no connectable scanner can
    see the device, :class:`~.ble_gatt.BleWriteCharacteristicError` when the
    device exposes nothing printable, and bleak's own errors on connect
    failure.
    """
    _require_bluetooth(hass)
    # Imported lazily: HA sets up the bluetooth integration during startup,
    # and these modules pull in the whole habluetooth stack. Matches the
    # late-import policy used for python-escpos and pyusb elsewhere.
    from bleak_retry_connector import (  # noqa: PLC0415
        BleakClientWithServiceCache,
        establish_connection,
    )

    device = async_ble_device(hass, address)
    if device is None:
        raise BleNotFoundError(
            f"No connectable Bluetooth adapter or proxy can currently reach {address}"
        )

    client = await establish_connection(
        BleakClientWithServiceCache,
        device,
        f"ESC/POS printer {address}",
        disconnected_callback=disconnected_callback,
        pair=pair,
    )
    try:
        characteristic = select_write_characteristic(client, write_uuid)
    except BleWriteCharacteristicError:
        # Nothing printable here — do not leave the link (and, over a proxy,
        # a scarce connection slot) held open on the way out.
        with contextlib.suppress(Exception):
            await client.disconnect()
        raise

    use_response = prefers_response(characteristic) if with_response is None else with_response
    max_chunk = max_write_size(client, characteristic, with_response=use_response)
    # Snapshot the GATT layout while the link is up. Diagnostics is the only
    # place a user can see which characteristics their printer exposes, and
    # it must not have to open a connection to do it.
    gatt_layout = describe_services(client)
    _LOGGER.debug(
        "BLE connected to %s char=%s response=%s max_chunk=%s paired=%s",
        sanitize_log_message(address),
        characteristic.uuid,
        use_response,
        max_chunk,
        pair,
    )
    return BleConnection(
        client,
        characteristic,
        address=address,
        with_response=use_response,
        max_chunk=max_chunk,
        gatt_layout=gatt_layout,
    )


class _BleTransport:
    """Sync byte-sink that funnels a whole receipt through one loop hop.

    python-escpos emits a write per ESC/POS attribute (align, bold, text,
    cut …). Hopping to the event loop for each one would be both slow and a
    good way to interleave with other loop work mid-receipt, so writes are
    coalesced here and released in :meth:`flush`.

    The transport does **not** own the connection: :meth:`close` leaves the
    GATT link up for the adapter's idle timer to reap. BLE connects cost
    seconds and consume a scarce proxy slot, so tearing the link down after
    every receipt would make back-to-back prints painfully slow.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        connection: BleConnection,
        *,
        chunk_delay_s: float,
    ) -> None:
        self._hass = hass
        self._connection = connection
        self._chunk_delay_s = chunk_delay_s
        self._buffer = bytearray()

    def write(self, data: bytes) -> None:
        """Buffer bytes; nothing reaches the radio until :meth:`flush`."""
        if data:
            self._buffer.extend(data)

    def _flush_timeout(self, payload_len: int) -> float:
        """Budget for one flush, scaled by how many chunks it will take."""
        chunks = max(1, -(-payload_len // max(1, self._connection.max_chunk)))
        return _FLUSH_BASE_TIMEOUT_S + chunks * (self._chunk_delay_s + 0.25)

    def flush(self) -> None:
        """Push the buffered receipt to the printer, raising on failure.

        Errors propagate: the adapter calls this on the success path so a
        write that fails here is reported as a failed print rather than
        silently swallowed.
        """
        if not self._buffer:
            return
        data = bytes(self._buffer)
        self._buffer.clear()
        future = asyncio.run_coroutine_threadsafe(
            self._connection.async_write(data, self._chunk_delay_s),
            self._hass.loop,
        )
        try:
            future.result(self._flush_timeout(len(data)))
        except TimeoutError:
            # Stop the orphaned write from continuing to push bytes at a
            # printer we have already given up on.
            future.cancel()
            raise

    def close(self) -> None:
        """Best-effort flush; the connection outlives the transport."""
        with contextlib.suppress(Exception):
            self.flush()


def open_ble_transport(
    hass: HomeAssistant,
    connection: BleConnection,
    *,
    chunk_delay_ms: int = 0,
) -> _BleTransport:
    """Return a byte-sink writing to ``connection``.

    This is the seam swapped by tests, mirroring ``open_rfcomm_transport``
    and ``open_serial_transport``. Unlike those, it takes an already-open
    connection rather than opening one, because the adapter caches the GATT
    link across prints.
    """
    return _BleTransport(hass, connection, chunk_delay_s=chunk_delay_ms / 1000.0)
