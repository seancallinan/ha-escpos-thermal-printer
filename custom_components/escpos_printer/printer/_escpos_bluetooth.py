"""python-escpos subclass that writes through an :class:`RfcommTransport`.

Thin alias over :mod:`._escpos_transport`, which carries the single
implementation shared by the RFCOMM, serial, and BLE transports. The names
here are kept because ``bluetooth_adapter`` imports ``make_bluetooth_escpos``
and ``tests/conftest.py`` invalidates the cached subclass through
``_get_bluetooth_escpos_cls.cache_clear()``. Both are aliases for the shared
objects, so clearing through either name clears the one real cache.
"""

from __future__ import annotations

from ._escpos_transport import _get_transport_escpos_cls, make_transport_escpos

# Aliases — same objects, so ``cache_clear()`` through this name clears the
# single shared cache used by every transport-backed printer.
_get_bluetooth_escpos_cls = _get_transport_escpos_cls
make_bluetooth_escpos = make_transport_escpos

__all__ = ["_get_bluetooth_escpos_cls", "make_bluetooth_escpos"]
