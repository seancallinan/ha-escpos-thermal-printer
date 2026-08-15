"""python-escpos subclass that writes through a swappable byte-sink transport.

python-escpos ships ``Network``, ``Usb``, ``Serial``, and ``File`` printer
classes, none of which fit the transports this integration needs: the
built-in ``Serial`` hard-imports ``pyserial`` (we use ``serialx`` for its
URL-based transports), and there is no Bluetooth-aware variant at all.
Rather than vendoring a fork, we build a minimal subclass of
``escpos.escpos.Escpos`` that delegates the abstract ``_raw`` byte-write to
a transport object satisfying :class:`ByteSinkTransport`.

Every non-network transport in this integration — RFCOMM
(``bluetooth_transport``), serialx (``serial_transport``), and BLE GATT
(``ble_transport``) — funnels through this one class. The
transport-specific modules (``_escpos_bluetooth``, ``_escpos_serial``,
``_escpos_ble``) are thin aliases kept for import stability.

The subclass is constructed lazily on first use and cached at module scope
(via ``functools.cache``) so each print operation does not pay the
class-creation cost. Tests that swap the ``escpos.escpos`` module call
``_get_transport_escpos_cls.cache_clear()`` to invalidate — reachable under
any of the alias names, since they all refer to this same cached function.
"""

from __future__ import annotations

import functools
from typing import Any, Protocol


class ByteSinkTransport(Protocol):
    """Minimal byte-sink interface used by the transport-backed printer.

    ``flush()`` is optional: transports that write straight to the wire
    (RFCOMM) omit it, while buffering transports (serialx, BLE) implement it
    so a deferred write failure can be surfaced instead of swallowed.
    """

    def write(self, data: bytes) -> None:
        """Write bytes to the underlying transport."""

    def close(self) -> None:
        """Close the underlying transport."""


@functools.cache
def _get_transport_escpos_cls() -> type[Any]:
    """Late-import ``escpos.escpos.Escpos`` and return a transport subclass.

    Late import keeps the integration loadable when python-escpos isn't yet
    available (HA startup ordering).
    """
    from escpos.escpos import Escpos  # noqa: PLC0415

    class _TransportEscpos(Escpos):
        """python-escpos subclass that writes through a byte-sink transport."""

        def __init__(self, transport: ByteSinkTransport, profile: Any | None) -> None:
            self._transport: ByteSinkTransport | None = transport
            super().__init__(profile=profile)

        def _raw(self, msg: bytes) -> None:
            if self._transport is None:
                raise OSError("Printer transport already closed")
            self._transport.write(msg)

        def flush(self) -> None:
            """Flush buffered bytes to the wire, raising on write failure.

            python-escpos itself never calls this; the buffering adapters
            (serial, BLE) call it on the success path — before the
            best-effort ``close`` — so a failed write is reported instead of
            silently swallowed. Transports without a ``flush`` (RFCOMM writes
            straight through) make this a no-op.
            """
            if self._transport is None:
                raise OSError("Printer transport already closed")
            transport_flush = getattr(self._transport, "flush", None)
            if transport_flush is not None:
                transport_flush()

        def _read(self) -> bytes:
            # These transports are write-only for ESC/POS in practice: RFCOMM
            # SPP and BLE GATT write characteristics carry no status channel,
            # and serial connections are wired one-way. Returning empty rather
            # than raising keeps python-escpos paths that opportunistically
            # read (e.g. during init) from blowing up; the adapters never
            # trigger status-read paths.
            return b""

        def close(self) -> None:
            if self._transport is not None:
                try:
                    self._transport.close()
                finally:
                    self._transport = None

    return _TransportEscpos


def make_transport_escpos(transport: ByteSinkTransport, profile: Any | None = None) -> Any:
    """Return a python-escpos printer wired to the given byte-sink transport."""
    cls = _get_transport_escpos_cls()
    return cls(transport, profile)
