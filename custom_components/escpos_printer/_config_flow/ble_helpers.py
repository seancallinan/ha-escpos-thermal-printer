"""BLE discovery and connectivity helpers for the config flow.

Unlike Bluetooth Classic, BLE needs no out-of-band pairing step for these
printers and no bluez D-Bus access: Home Assistant's ``bluetooth``
integration already maintains the set of devices it can see, across the host
radio *and* every configured Bluetooth proxy. This module reads that set and
probes connectivity by opening a real GATT link.

The probe is deliberately a full connect + characteristic resolve rather
than a "have we heard an advertisement" check. Discovering at setup time
that a device is unreachable, or exposes nothing writable, is far kinder
than accepting the entry and failing on the user's first print.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..const import BLE_MANUAL_ENTRY_KEY, BLE_SHOW_ALL_KEY
from ..printer import ble_transport
from ..printer.ble_gatt import KNOWN_WRITE_UUIDS, BleWriteCharacteristicError
from ..security import sanitize_log_message, validate_bluetooth_mac

_LOGGER = logging.getLogger(__name__)

# ESC @ — reset the printer to its power-on state. The standard ESC/POS
# no-op: it advances no paper and prints nothing, which makes it the right
# payload for proving the write path works before accepting an entry.
_ESCPOS_INITIALIZE = b"\x1b\x40"

# Advertised-name fragments typical of BLE thermal printers. BLE has no
# Class-of-Device field and these printers rarely advertise a service UUID
# in their advertisement (the printable characteristic only shows up after
# service discovery), so the name is the only pre-connect signal available.
# Used purely to sort likely printers to the top of the picker — never to
# exclude a device the user can still reach via "show all".
_PRINTER_NAME_HINTS: tuple[str, ...] = (
    "print",
    "pos",
    "thermal",
    "receipt",
    "phomemo",
    "goojprt",
    "cashino",
    "netum",
    "mtp-",
    "rp-",
    "pt-",
    "gb0",
    "gt0",
    "mx0",
)


def _normalize_ble_address(address: Any) -> str | None:
    """Return a canonical ``XX:XX:XX:XX:XX:XX`` address, or ``None`` if invalid.

    BLE addresses share the MAC shape, so the existing validator applies.
    Return-None rather than raise, because discovery loops want to skip bad
    entries silently.
    """
    if not isinstance(address, str):
        return None
    try:
        return validate_bluetooth_mac(address)
    except HomeAssistantError:
        return None


def _generate_ble_unique_id(address: str) -> str:
    """Generate a unique ID for a BLE printer.

    Prefixed ``ble:`` rather than ``bt:`` so the same physical printer can be
    configured over both transports (a dual-mode printer is a real thing) and
    so a Classic entry never collides with a BLE one.
    """
    return f"ble:{address.lower()}"


_BLE_ERROR_KEY_MAP: dict[str, str] = {
    "not_found": "ble_not_found",
    "no_write_char": "ble_no_write_char",
    "connect_failed": "ble_connect_failed",
    "no_bluetooth": "ble_no_bluetooth",
    "needs_pairing": "ble_needs_pairing",
    "no_scanners": "ble_no_scanners",
}


def _ble_error_to_key(error_code: str | None) -> str:
    """Convert a BLE error code to a strings.json error key."""
    return _BLE_ERROR_KEY_MAP.get(error_code or "", "cannot_connect_ble")


def _is_ble_printer_candidate(device: dict[str, Any]) -> bool:
    """Heuristically flag a discovered device as printer-like by name."""
    name = str(device.get("name") or "").lower()
    return any(hint in name for hint in _PRINTER_NAME_HINTS)


async def _list_ble_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Enumerate BLE devices HA can currently *connect* to.

    ``connectable=True`` filters out devices only seen by passive scanners —
    an ESPHome proxy without ``active: true`` can report advertisements it
    could never open a GATT link to, and offering those in the picker would
    produce a confusing failure at the probe step.
    """
    if not ble_transport.bluetooth_ready(hass):
        # No bluetooth integration at all — the flow routes to the
        # ble_no_devices step, which explains how to add an adapter or proxy.
        return []

    devices: list[dict[str, Any]] = []
    for service_info in ble_transport.async_discovered_devices(hass):
        address = _normalize_ble_address(service_info.address)
        if address is None:
            continue
        name = (service_info.name or "").strip()
        rssi = service_info.rssi
        label = f"{name} ({address})" if name else address
        if rssi is not None:
            label = f"{label} {rssi} dBm"
        devices.append(
            {
                "address": address,
                "name": name or address,
                "label": label,
                "rssi": rssi,
                "_choice_key": address,
            }
        )
    # Strongest signal first: the printer the user is setting up is usually
    # the one they are standing next to.
    devices.sort(key=lambda d: (d["rssi"] is None, -(d["rssi"] or 0)))
    return devices


def _build_ble_device_choices(
    devices: list[dict[str, Any]], *, printers_only: bool = True
) -> dict[str, str]:
    """Build the dropdown for the ble_select step.

    Name-matching is a weak signal, so unlike the Classic picker this always
    offers "show all" when filtering is active — a printer with a generic
    name (plenty advertise as "BT-Printer" or a bare MAC) must not be
    unreachable through the UI.
    """
    candidates = (
        [d for d in devices if _is_ble_printer_candidate(d)] if printers_only else list(devices)
    )
    choices: dict[str, str] = {d["_choice_key"]: d["label"] for d in candidates}
    if printers_only and len(candidates) < len(devices):
        choices[BLE_SHOW_ALL_KEY] = "Show all discovered BLE devices..."
    choices[BLE_MANUAL_ENTRY_KEY] = "Manual address entry..."
    return choices


async def _can_connect_ble(
    hass: HomeAssistant,
    address: str,
    write_uuid: str | None,
    with_response: bool | None,
    pair: bool = False,
) -> tuple[bool, str | None, str | None]:
    """Probe a BLE printer by opening a GATT link and writing to it.

    Returns ``(success, error_code, resolved_write_uuid)``. The resolved UUID
    is fed back into the entry so a later firmware quirk or reordered service
    table cannot silently move the print target — and so the diagnostics
    download records what was actually negotiated.

    The probe **writes**, it does not merely connect. Some printers accept
    the connection and resolve their characteristic normally, then reject the
    first actual write with ATT 0x08 because the link isn't bonded. A
    connect-only probe reports those as healthy and the user discovers the
    truth on their first print. ``ESC @`` (initialise) is the harmless
    payload for this: it resets printer state and emits no paper.
    """
    connection = None
    try:
        connection = await ble_transport.async_connect_ble(
            hass,
            address,
            write_uuid=write_uuid,
            with_response=with_response,
            pair=pair,
        )
        await connection.async_write(_ESCPOS_INITIALIZE, 0.0)
    except Exception as exc:
        code = _probe_error_code(hass, exc)
        _LOGGER.debug(
            "BLE probe failed for %s (%s): %s",
            sanitize_log_message(address),
            code,
            sanitize_log_message(str(exc)),
        )
        return False, code, None
    else:
        return True, None, connection.characteristic_uuid
    finally:
        if connection is not None:
            await connection.async_disconnect()


def _probe_error_code(hass: HomeAssistant, exc: Exception) -> str:
    """Map a probe failure to a stable error code.

    The scanner-pool check is what keeps an infrastructure outage from
    being reported as a printer problem: Home Assistant caches discovered
    devices for a while, so a stale handle still resolves after every proxy
    has gone offline, and the failure only surfaces at connect time.
    Blaming range or connection slots there sends the user hunting in
    entirely the wrong place.
    """
    if isinstance(exc, ble_transport.BluetoothUnavailableError):
        return "no_bluetooth"
    if isinstance(exc, BleWriteCharacteristicError):
        return "no_write_char"
    if isinstance(exc, ble_transport.BleAuthorizationError):
        return "needs_pairing"
    if ble_transport.connectable_scanner_count(hass) == 0:
        return "no_scanners"
    if isinstance(exc, ble_transport.BleNotFoundError):
        return "not_found"
    return "connect_failed"


def _known_uuid_choices() -> dict[str, str]:
    """Labelled dropdown of the write UUIDs we auto-detect, plus 'auto'."""
    return {
        "": "Auto-detect (recommended)",
        KNOWN_WRITE_UUIDS[0]: "0xFF02 — cat printers, Goojprt, MTP series",
        KNOWN_WRITE_UUIDS[1]: "0x2AF1 — Phomemo, POS58-BLE, generic clones",
        KNOWN_WRITE_UUIDS[2]: "Nordic UART — nRF-based printers",
        KNOWN_WRITE_UUIDS[3]: "Microchip transparent UART — BM70/RN487x",
        KNOWN_WRITE_UUIDS[4]: "0xFFE1 — HM-10 style serial bridges",
    }
