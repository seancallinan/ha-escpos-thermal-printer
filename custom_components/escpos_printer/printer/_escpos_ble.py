"""python-escpos subclass that writes through a BLE GATT transport.

Thin alias over :mod:`._escpos_transport`, which carries the single
implementation shared by the RFCOMM, serial, and BLE transports.
"""

from __future__ import annotations

from ._escpos_transport import _get_transport_escpos_cls, make_transport_escpos

# Aliases — same objects, so ``cache_clear()`` through this name clears the
# single shared cache used by every transport-backed printer.
_get_ble_escpos_cls = _get_transport_escpos_cls
make_ble_escpos = make_transport_escpos

__all__ = ["_get_ble_escpos_cls", "make_ble_escpos"]
