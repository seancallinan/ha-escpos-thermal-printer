"""python-escpos subclass that writes through a :class:`SerialTransport`.

Thin alias over :mod:`._escpos_transport`, which carries the single
implementation shared by the RFCOMM, serial, and BLE transports. The names
here are kept because ``serial_adapter`` imports ``make_serial_escpos`` and
``tests/test_serial_adapter.py`` patches it by that path.
"""

from __future__ import annotations

from ._escpos_transport import _get_transport_escpos_cls, make_transport_escpos

# Aliases — same objects, so ``cache_clear()`` through this name clears the
# single shared cache used by every transport-backed printer.
_get_serial_escpos_cls = _get_transport_escpos_cls
make_serial_escpos = make_transport_escpos

__all__ = ["_get_serial_escpos_cls", "make_serial_escpos"]
