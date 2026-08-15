"""Tests for the BLE signal-strength sensor and the BLE UUID validator."""

from unittest.mock import patch

from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.escpos_printer.const import (
    CONF_BLE_ADDRESS,
    CONF_BT_MAC,
    CONF_CONNECTION_TYPE,
    CONNECTION_TYPE_BLE,
    CONNECTION_TYPE_BLUETOOTH,
    CONNECTION_TYPE_NETWORK,
    DOMAIN,
)
from custom_components.escpos_printer.printer import ble_transport
from custom_components.escpos_printer.security import validate_ble_uuid
from custom_components.escpos_printer.sensor import (
    BlePrinterSignalSensor,
    async_setup_entry,
)

_FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"


def _ble_entry(address="AA:BB:CC:DD:EE:FF"):
    return MockConfigEntry(
        domain=DOMAIN,
        title="BLE Printer",
        data={
            CONF_CONNECTION_TYPE: CONNECTION_TYPE_BLE,
            CONF_BLE_ADDRESS: address,
        },
        unique_id="ble:aa:bb:cc:dd:ee:ff",
    )


class TestValidateBleUuid:
    """UUID normalization shared by the config flow and characteristic lookup."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (_FF02, _FF02),
            (_FF02.upper(), _FF02),
            (f"  {_FF02}  ", _FF02),
            # 16-bit shorthand — how printer docs and forum posts quote these.
            ("ff02", _FF02),
            ("FF02", _FF02),
            ("0xff02", _FF02),
            ("0xFF02", _FF02),
            # 32-bit shorthand.
            ("0000ff02", _FF02),
            ("00002af1", "00002af1-0000-1000-8000-00805f9b34fb"),
            # A vendor UUID outside the SIG base must survive untouched.
            (
                "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
                "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
            ),
        ],
    )
    def test_normalizes_accepted_forms(self, raw, expected):
        assert validate_ble_uuid(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "zzzz",
            "ff0",  # 3 hex digits: neither 16- nor 32-bit shorthand
            "ff02-0000",
            "0000ff02-0000-1000-8000",
            "0000ff02-0000-1000-8000-00805f9b34fbxx",
            "gggggggg-0000-1000-8000-00805f9b34fb",
        ],
    )
    def test_rejects_malformed_uuids(self, raw):
        with pytest.raises(HomeAssistantError, match="Invalid BLE UUID"):
            validate_ble_uuid(raw)

    @pytest.mark.parametrize("raw", [None, 42, b"ff02"])
    def test_rejects_non_strings(self, raw):
        with pytest.raises(HomeAssistantError, match="must be a string"):
            validate_ble_uuid(raw)


class TestSignalSensorSetup:
    """The sensor is added only for BLE entries."""

    async def test_added_for_ble_entries(self, hass):
        entry = _ble_entry()
        entry.add_to_hass(hass)
        added = []
        await async_setup_entry(hass, entry, lambda entities, **_: added.extend(entities))
        assert any(isinstance(e, BlePrinterSignalSensor) for e in added)

    async def test_not_added_for_other_transports(self, hass):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_CONNECTION_TYPE: CONNECTION_TYPE_NETWORK},
        )
        entry.add_to_hass(hass)
        added = []
        await async_setup_entry(hass, entry, lambda entities, **_: added.extend(entities))
        assert not any(isinstance(e, BlePrinterSignalSensor) for e in added)

    async def test_not_added_for_bluetooth_classic(self, hass):
        """Classic entries get the bluez battery sensor, not an RSSI one."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_CONNECTION_TYPE: CONNECTION_TYPE_BLUETOOTH,
                CONF_BT_MAC: "AA:BB:CC:DD:EE:FF",
            },
        )
        entry.add_to_hass(hass)
        added = []
        await async_setup_entry(hass, entry, lambda entities, **_: added.extend(entities))
        assert not any(isinstance(e, BlePrinterSignalSensor) for e in added)

    async def test_not_added_without_an_address(self, hass):
        entry = _ble_entry(address="")
        entry.add_to_hass(hass)
        added = []
        await async_setup_entry(hass, entry, lambda entities, **_: added.extend(entities))
        assert not any(isinstance(e, BlePrinterSignalSensor) for e in added)


class TestSignalSensorUpdate:
    """Reading RSSI from advertisement data."""

    @pytest.fixture
    def sensor(self, hass):
        entry = _ble_entry()
        entry.add_to_hass(hass)
        sensor = BlePrinterSignalSensor(entry, "AA:BB:CC:DD:EE:FF")
        sensor.hass = hass
        return sensor

    def test_diagnostic_and_disabled_by_default(self, sensor):
        """Most users don't need an RSSI entity cluttering the device page."""
        assert sensor._attr_entity_registry_enabled_default is False
        assert sensor.unique_id.endswith("_signal_strength")

    async def test_reports_rssi_from_last_advertisement(self, sensor, hass):
        hass.config.components.add("bluetooth")
        advert = type("Advert", (), {"rssi": -63})()
        with patch.object(ble_transport, "async_last_advertisement", return_value=advert):
            await sensor.async_update()
        assert sensor.available is True
        assert sensor.native_value == -63

    async def test_unavailable_when_nothing_heard(self, sensor, hass):
        hass.config.components.add("bluetooth")
        with patch.object(ble_transport, "async_last_advertisement", return_value=None):
            await sensor.async_update()
        assert sensor.available is False
        assert sensor.native_value is None

    async def test_unavailable_when_bluetooth_not_set_up(self, sensor, hass):
        """Must not raise into HA's update loop when the stack is absent."""
        await sensor.async_update()
        assert sensor.available is False
        assert sensor.native_value is None

    async def test_recovers_after_the_printer_comes_back(self, sensor, hass):
        hass.config.components.add("bluetooth")
        with patch.object(ble_transport, "async_last_advertisement", return_value=None):
            await sensor.async_update()
        assert sensor.available is False

        advert = type("Advert", (), {"rssi": -70})()
        with patch.object(ble_transport, "async_last_advertisement", return_value=advert):
            await sensor.async_update()
        assert sensor.available is True
        assert sensor.native_value == -70
