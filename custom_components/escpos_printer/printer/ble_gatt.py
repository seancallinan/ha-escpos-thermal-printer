"""GATT-layer concerns for BLE ESC/POS printers.

Kept separate from ``ble_transport`` so characteristic selection is unit
testable against a plain stub client, with no event loop, no HA bluetooth
stack, and no transport in the picture.

BLE thermal printers have no equivalent of the RFCOMM Serial Port Profile —
there is no single standard "this is the printer data channel" UUID. In
practice the market clusters around a handful of vendor conventions
(:data:`KNOWN_WRITE_UUIDS`), so we prefer those in order and fall back to
"any writable characteristic" for the long tail of clones. The user can
always override via the config flow when auto-detection picks wrong.
"""

from __future__ import annotations

from typing import Any

from ..security import validate_ble_uuid

# Write-characteristic UUIDs known to carry ESC/POS payloads, most specific
# convention first. Order matters: a printer can expose several writable
# characteristics (a vendor control channel alongside the data channel), and
# picking the wrong one produces a silent no-op print.
#
# - 0xFF02 (in the 0xFF00 service): cat printers (GB01/GB02/GT01), several
#   Goojprt and MTP-series models.
# - 0x2AF1 (in the 0x18F0 service): the most common generic Chinese BLE
#   thermal printer stack — Phomemo, POS58-BLE and many unbranded clones.
# - Nordic UART RX: printers built on an nRF module.
# - Microchip / ISSC transparent UART: the BM70/RN487x family.
# - 0xFFE1 (in the 0xFFE0 service): HM-10 style serial bridges.
KNOWN_WRITE_UUIDS: tuple[str, ...] = (
    "0000ff02-0000-1000-8000-00805f9b34fb",
    "00002af1-0000-1000-8000-00805f9b34fb",
    "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
    "49535343-8841-43f4-a8d4-ecbe34729bb3",
    "0000ffe1-0000-1000-8000-00805f9b34fb",
)

# bleak property names that mean "this characteristic accepts a write".
_WRITABLE_PROPERTIES = frozenset({"write", "write-without-response"})


class BleWriteCharacteristicError(Exception):
    """Raised when no usable write characteristic can be resolved.

    Distinct from bleak's connection errors: the link is up and service
    discovery succeeded, but the device exposes nothing we can print to (or
    the user's explicit override does not exist on this device).
    """


def _iter_characteristics(client: Any) -> list[Any]:
    """Flatten the client's discovered services into a characteristic list."""
    characteristics: list[Any] = []
    for service in client.services:
        characteristics.extend(service.characteristics)
    return characteristics


def is_writable(characteristic: Any) -> bool:
    """Return True if the characteristic accepts writes of either kind."""
    return bool(_WRITABLE_PROPERTIES.intersection(characteristic.properties or ()))


def select_write_characteristic(client: Any, override_uuid: str | None = None) -> Any:
    """Resolve the characteristic ESC/POS bytes should be written to.

    Resolution order:

    1. ``override_uuid`` when set — an exact match is required, and a missing
       or non-writable match is an error rather than a silent fallback. A user
       who typed a UUID gets told it was wrong instead of watching us quietly
       print somewhere else.
    2. The first entry of :data:`KNOWN_WRITE_UUIDS` the device exposes.
    3. The first writable characteristic in discovery order.

    Raises :class:`BleWriteCharacteristicError` when nothing qualifies.
    """
    characteristics = _iter_characteristics(client)

    if override_uuid:
        wanted = validate_ble_uuid(override_uuid)
        for characteristic in characteristics:
            if characteristic.uuid.lower() == wanted:
                if not is_writable(characteristic):
                    raise BleWriteCharacteristicError(
                        f"Characteristic {wanted} exists but is not writable "
                        f"(properties: {', '.join(characteristic.properties or ())})"
                    )
                return characteristic
        raise BleWriteCharacteristicError(
            f"Characteristic {wanted} not found on this device. "
            "Check the diagnostics download for the characteristics it does expose."
        )

    by_uuid = {c.uuid.lower(): c for c in characteristics}
    for known in KNOWN_WRITE_UUIDS:
        candidate = by_uuid.get(known)
        if candidate is not None and is_writable(candidate):
            return candidate

    for characteristic in characteristics:
        if is_writable(characteristic):
            return characteristic

    raise BleWriteCharacteristicError(
        "Device exposes no writable GATT characteristic; it is probably not a BLE ESC/POS printer."
    )


def prefers_response(characteristic: Any) -> bool:
    """Return True if this characteristic supports acknowledged writes.

    Write-with-response is the safer default for printing: it gives real
    per-packet backpressure, which cheap printer firmware needs when a
    full-width image arrives. Only fall back to write-without-response when
    the characteristic does not offer the acknowledged form.
    """
    return "write" in (characteristic.properties or ())


def max_write_size(client: Any, characteristic: Any, *, with_response: bool) -> int:
    """Return the largest payload that fits in a single GATT write.

    For unacknowledged writes bleak exposes
    ``max_write_without_response_size``, which already accounts for the
    negotiated MTU (and, over an ESPHome proxy, for whatever the proxy
    negotiated). For acknowledged writes the limit is ``MTU - 3`` for the ATT
    opcode and handle. Falls back to the BLE default MTU of 23 (20 bytes of
    payload) when the client cannot report one.
    """
    if not with_response:
        size = getattr(characteristic, "max_write_without_response_size", 0) or 0
        if size > 0:
            return int(size)
    mtu = getattr(client, "mtu_size", 0) or 0
    if mtu > 3:
        return int(mtu) - 3
    return 20


def describe_services(client: Any) -> list[dict[str, Any]]:
    """Return a flat, JSON-safe dump of the device's GATT layout.

    Surfaced in the diagnostics download so an unrecognised printer can be
    identified without the user attaching a BLE sniffer — the characteristic
    they need for the override field is in this list.
    """
    return [
        {
            "service_uuid": service.uuid,
            "characteristic_uuid": characteristic.uuid,
            "handle": characteristic.handle,
            "properties": sorted(characteristic.properties or ()),
            "writable": is_writable(characteristic),
            "known_printer_uuid": characteristic.uuid.lower() in KNOWN_WRITE_UUIDS,
        }
        for service in client.services
        for characteristic in service.characteristics
    ]
