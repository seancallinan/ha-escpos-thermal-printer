"""Tests for the python-escpos subclass shared by all byte-sink transports.

One implementation backs the RFCOMM, serial, and BLE transports; the
per-transport modules are aliases. These tests pin the behaviour they all
rely on, in particular that a closed transport fails loudly rather than
silently discarding a receipt.
"""

import pytest

from custom_components.escpos_printer.printer import _escpos_ble, _escpos_bluetooth, _escpos_serial
from custom_components.escpos_printer.printer._escpos_transport import (
    _get_transport_escpos_cls,
    make_transport_escpos,
)


class RecordingTransport:
    """Byte sink with the optional flush() implemented."""

    def __init__(self):
        self.written = bytearray()
        self.flushes = 0
        self.closed = 0

    def write(self, data):
        self.written.extend(data)

    def flush(self):
        self.flushes += 1

    def close(self):
        self.closed += 1


class UnflushableTransport:
    """Byte sink without flush() — how the RFCOMM transport is shaped."""

    def __init__(self):
        self.written = bytearray()
        self.closed = 0

    def write(self, data):
        self.written.extend(data)

    def close(self):
        self.closed += 1


class TestAliasesShareOneImplementation:
    """The per-transport modules must stay aliases, not copies.

    tests/conftest.py invalidates the cached subclass through the
    *bluetooth* name; if these ever diverged, the serial and BLE caches
    would keep a stale base class after the fake escpos module is swapped.
    """

    def test_factories_are_the_same_object(self):
        assert _escpos_bluetooth.make_bluetooth_escpos is make_transport_escpos
        assert _escpos_serial.make_serial_escpos is make_transport_escpos
        assert _escpos_ble.make_ble_escpos is make_transport_escpos

    def test_caches_are_the_same_object(self):
        assert _escpos_bluetooth._get_bluetooth_escpos_cls is _get_transport_escpos_cls
        assert _escpos_serial._get_serial_escpos_cls is _get_transport_escpos_cls
        assert _escpos_ble._get_ble_escpos_cls is _get_transport_escpos_cls


class TestRawWrites:
    """_raw is the seam python-escpos drives."""

    def test_raw_forwards_to_the_transport(self):
        transport = RecordingTransport()
        printer = make_transport_escpos(transport)
        printer._raw(b"ESC@")
        assert bytes(transport.written) == b"ESC@"

    def test_raw_after_close_raises(self):
        """Silently discarding a receipt would be far worse than raising."""
        transport = RecordingTransport()
        printer = make_transport_escpos(transport)
        printer.close()
        with pytest.raises(OSError, match="already closed"):
            printer._raw(b"data")

    def test_read_returns_empty(self):
        """These transports are write-only; python-escpos may still probe."""
        printer = make_transport_escpos(RecordingTransport())
        assert printer._read() == b""


class TestFlush:
    """flush() surfaces deferred write failures on buffering transports."""

    def test_flush_delegates_when_supported(self):
        transport = RecordingTransport()
        printer = make_transport_escpos(transport)
        printer.flush()
        assert transport.flushes == 1

    def test_flush_is_a_noop_without_transport_support(self):
        """RFCOMM writes straight through, so it has nothing to flush."""
        printer = make_transport_escpos(UnflushableTransport())
        printer.flush()  # must not raise

    def test_flush_after_close_raises(self):
        printer = make_transport_escpos(RecordingTransport())
        printer.close()
        with pytest.raises(OSError, match="already closed"):
            printer.flush()

    def test_flush_errors_propagate(self):
        class Exploding(RecordingTransport):
            def flush(self):
                raise OSError("write failed at flush")

        printer = make_transport_escpos(Exploding())
        with pytest.raises(OSError, match="write failed at flush"):
            printer.flush()


class TestClose:
    """close() is called on cleanup paths and must be safe to repeat."""

    def test_close_closes_the_transport(self):
        transport = RecordingTransport()
        printer = make_transport_escpos(transport)
        printer.close()
        assert transport.closed == 1

    def test_close_is_idempotent(self):
        transport = RecordingTransport()
        printer = make_transport_escpos(transport)
        printer.close()
        printer.close()
        assert transport.closed == 1

    def test_transport_is_released_even_when_close_raises(self):
        """A transport that fails to close must not stay referenced."""

        class Exploding(RecordingTransport):
            def close(self):
                raise OSError("close failed")

        printer = make_transport_escpos(Exploding())
        with pytest.raises(OSError):
            printer.close()
        assert printer._transport is None
