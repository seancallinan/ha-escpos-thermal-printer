"""Printer adapter package for ESC/POS thermal printers.

This package provides adapters for communicating with ESC/POS thermal printers
over network (TCP/IP), USB, Bluetooth Classic / RFCOMM, BLE (GATT), and
serial connections.
"""

from __future__ import annotations

from .base_adapter import EscposPrinterAdapterBase, profile_width_issue_id
from .ble_adapter import BlePrinterAdapter
from .bluetooth_adapter import BluetoothPrinterAdapter
from .config import (
    BasePrinterConfig,
    BlePrinterConfig,
    BluetoothPrinterConfig,
    NetworkPrinterConfig,
    PrinterConfig,
    PrinterConfigTypes,
    SerialPrinterConfig,
    UsbPrinterConfig,
)
from .factory import create_printer_adapter
from .network_adapter import NetworkPrinterAdapter
from .serial_adapter import SerialPrinterAdapter
from .usb_adapter import UsbPrinterAdapter

# Legacy alias for backward compatibility
EscposPrinterAdapter = NetworkPrinterAdapter

__all__ = [
    "BasePrinterConfig",
    "BlePrinterAdapter",
    "BlePrinterConfig",
    "BluetoothPrinterAdapter",
    "BluetoothPrinterConfig",
    "EscposPrinterAdapter",
    "EscposPrinterAdapterBase",
    "NetworkPrinterAdapter",
    "NetworkPrinterConfig",
    "PrinterConfig",
    "PrinterConfigTypes",
    "SerialPrinterAdapter",
    "SerialPrinterConfig",
    "UsbPrinterAdapter",
    "UsbPrinterConfig",
    "create_printer_adapter",
    "profile_width_issue_id",
]
