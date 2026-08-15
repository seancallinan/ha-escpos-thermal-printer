"""Sensor platform — per-transport diagnostic sensors for a printer entry.

* Battery level for Bluetooth Classic printers, when bluez tracks it. Most
  cheap thermal printers don't expose ``org.bluez.Battery1`` so the entity
  stays unavailable; portable models (Phomemo M02, newer Netum firmware,
  some Cashino models) do, giving users a real "low battery" signal.
* Signal strength for BLE printers, read from the advertisement data HA's
  bluetooth integration already holds. Useful for deciding whether a
  printer needs a Bluetooth proxy closer to it.
* Paper status for network/USB printers, and the last-image-print
  diagnostic for every entry.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from .bluez import query_bt_battery_percentage
from .const import (
    CONF_BLE_ADDRESS,
    CONF_BT_MAC,
    CONF_CONNECTION_TYPE,
    CONNECTION_TYPE_BLE,
    CONNECTION_TYPE_BLUETOOTH,
    CONNECTION_TYPE_NETWORK,
    CONNECTION_TYPE_USB,
)
from .device import build_device_info

# Module-qualified rather than `from ... import name`: these are the
# patchable seam for HA's bluetooth API (see printer/ble_transport), and
# binding the names here would sidestep it.
from .printer import ble_transport

_LOGGER = logging.getLogger(__name__)

# Battery polling cadence is set by _attr_should_poll on the entity.
PARALLEL_UPDATES = 0

# Both polling sensors below were documented as "every 5 minutes" but
# nothing enforced it -- HA's entity-component default poll interval is
# 30s, so a full D-Bus GetManagedObjects round-trip (battery) or a fresh
# printer connection (paper status) was happening 10x more often than
# intended.
SCAN_INTERVAL = timedelta(minutes=5)

# python-escpos paper_status() return codes → enum sensor options.
_PAPER_STATUS_OPTIONS = {2: "ok", 1: "low", 0: "out"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Any
) -> None:
    """Add a battery sensor for Bluetooth printers and a last-print sensor.

    Network and USB printers don't have battery state to report — battery
    sensor only for the BT branch. The last-print diagnostic sensor is
    always added so users get a live view of the image pipeline.
    """
    sensors: list[SensorEntity] = [LastImagePrintSensor(entry)]

    if entry.data.get(CONF_CONNECTION_TYPE) == CONNECTION_TYPE_BLUETOOTH:
        mac = entry.data.get(CONF_BT_MAC, "")
        if mac:
            sensors.append(BluetoothPrinterBatterySensor(entry, mac))

    if entry.data.get(CONF_CONNECTION_TYPE) == CONNECTION_TYPE_BLE:
        address = entry.data.get(CONF_BLE_ADDRESS, "")
        if address:
            sensors.append(BlePrinterSignalSensor(entry, address))

    # Paper status needs a real read channel (DLE EOT response); the
    # Bluetooth/serial transports are write-only, and python-escpos
    # reports an empty read as "plenty of paper" — a false OK.
    if entry.data.get(CONF_CONNECTION_TYPE, CONNECTION_TYPE_NETWORK) in (
        CONNECTION_TYPE_NETWORK,
        CONNECTION_TYPE_USB,
    ):
        sensors.append(PaperStatusSensor(entry))

    async_add_entities(sensors, update_before_add=True)


class LastImagePrintSensor(SensorEntity):
    """Diagnostic sensor exposing the last image-print outcome.

    State is the count of successful image prints since startup; the
    interesting per-print fields (source kind, decoded dims, slice
    count, last error class) ride along as attributes so users can
    build dashboards without parsing diagnostics downloads.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_image_print"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_should_poll = True
    _unrecorded_attributes = frozenset(
        {
            "total_failures",
            "last_source_kind",
            "last_decoded_dims",
            "last_decoded_bytes",
            "last_slice_count",
            "last_error_class",
        }
    )

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_last_image_print"

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self._entry)

    async def async_update(self) -> None:
        """Pull the in-memory ImageStats snapshot off the adapter."""
        runtime = getattr(self._entry, "runtime_data", None)
        adapter = getattr(runtime, "adapter", None) if runtime else None
        stats = getattr(adapter, "_image_stats", None)
        if stats is None:
            self._attr_available = False
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        self._attr_available = True
        self._attr_native_value = stats.total_prints
        self._attr_extra_state_attributes = {
            "total_failures": stats.total_failures,
            "last_source_kind": stats.last_source_kind,
            "last_decoded_dims": list(stats.last_decoded_dims) if stats.last_decoded_dims else None,
            "last_decoded_bytes": stats.last_decoded_bytes,
            "last_slice_count": stats.last_slice_count,
            "last_error_class": stats.last_error_class,
        }


class BluetoothPrinterBatterySensor(SensorEntity):
    """Battery percentage for a paired BT printer (when bluez exposes it)."""

    _attr_has_entity_name = True
    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    # Battery level changes slowly; HA's default 30s isn't worth the D-Bus
    # round-trip cost. Update every 5 minutes.
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry, mac: str) -> None:
        self._entry = entry
        self._mac = mac
        self._attr_unique_id = f"{entry.entry_id}_battery"
        self._attr_available = False

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self._entry)

    async def async_update(self) -> None:
        """Poll bluez for the current battery percentage."""
        try:
            percentage = await query_bt_battery_percentage(self._mac)
        except Exception as exc:  # defensive; bluez can throw anything
            _LOGGER.debug("Battery query failed for %s: %s", self._entry.entry_id, exc)
            self._attr_available = False
            return
        if percentage is None:
            # Either bluez doesn't track this device, or the device doesn't
            # expose Battery1 (typical for cheap thermal printers).
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = percentage


class BlePrinterSignalSensor(SensorEntity):
    """Advertisement RSSI for a BLE printer.

    Reads whatever HA's bluetooth integration last heard — from the host
    radio or from any Bluetooth proxy — so it costs nothing but a dict
    lookup and never wakes the printer. Unavailable means nothing has heard
    an advertisement recently, which is the same signal the connectivity
    binary_sensor reports.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "signal_strength"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry, address: str) -> None:
        self._entry = entry
        self._address = address
        self._attr_unique_id = f"{entry.entry_id}_signal_strength"
        self._attr_available = False

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self._entry)

    async def async_update(self) -> None:
        """Read the last advertisement RSSI from the bluetooth integration."""
        if not ble_transport.bluetooth_ready(self.hass):
            self._attr_available = False
            self._attr_native_value = None
            return

        service_info = ble_transport.async_last_advertisement(self.hass, self._address)
        if service_info is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = service_info.rssi


class PaperStatusSensor(SensorEntity):
    """Paper sensor status via the ESC/POS real-time DLE EOT query.

    Lets users automate on "paper low" / "paper out" (issue #109).
    Network and USB printers only — see async_setup_entry.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "paper_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "low", "out"]
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_paper_status"
        self._attr_available = False

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self._entry)

    async def async_update(self) -> None:
        """Query the printer for its paper sensor state."""
        runtime = getattr(self._entry, "runtime_data", None)
        adapter = getattr(runtime, "adapter", None) if runtime else None
        if adapter is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        status = await adapter.get_paper_status(self.hass)
        value = _PAPER_STATUS_OPTIONS.get(status)
        if value is None:
            # Printer unreachable, query failed, or an unexpected code.
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = value
