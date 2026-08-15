"""Tests for the BLE (GATT) config flow."""

from unittest.mock import patch

import pytest

from custom_components.escpos_printer._config_flow.ble_helpers import (
    _ble_error_to_key,
    _build_ble_device_choices,
    _can_connect_ble,
    _generate_ble_unique_id,
    _is_ble_printer_candidate,
    _list_ble_devices,
    _normalize_ble_address,
)
from custom_components.escpos_printer._config_flow.ble_steps import (
    SECTION_BLE_ADVANCED,
    _parse_with_response,
)
from custom_components.escpos_printer.capabilities import PROFILE_AUTO
from custom_components.escpos_printer.config_flow import EscposConfigFlow
from custom_components.escpos_printer.const import (
    CONF_BLE_ADDRESS,
    CONF_BLE_DEVICE,
    CONF_BLE_PAIR,
    CONF_BLE_WITH_RESPONSE,
    CONF_BLE_WRITE_UUID,
    CONF_CONNECTION_TYPE,
    CONF_PROFILE,
    CONNECTION_TYPE_BLE,
)
from custom_components.escpos_printer.printer import ble_transport
from custom_components.escpos_printer.printer.ble_gatt import BleWriteCharacteristicError

_FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"
_2AF1 = "00002af1-0000-1000-8000-00805f9b34fb"


class FakeAdvert:
    """Stand-in for a BluetoothServiceInfoBleak."""

    def __init__(self, address, name, rssi=-60):
        self.address = address
        self.name = name
        self.rssi = rssi


@pytest.fixture
def mock_ble_devices():
    """Two discovered devices whose names read as printers."""
    return [
        {
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "Netum NT-1809DD",
            "label": "Netum NT-1809DD (AA:BB:CC:DD:EE:FF) -55 dBm",
            "rssi": -55,
            "_choice_key": "AA:BB:CC:DD:EE:FF",
        },
        {
            "address": "11:22:33:44:55:66",
            "name": "POS58 Printer",
            "label": "POS58 Printer (11:22:33:44:55:66) -70 dBm",
            "rssi": -70,
            "_choice_key": "11:22:33:44:55:66",
        },
    ]


def _flow(hass):
    flow = EscposConfigFlow()
    flow.hass = hass
    # A real flow gets a mutable context from the manager; steps that reach
    # async_set_unique_id write into it.
    flow.context = {"source": "user"}
    flow._user_data = {CONF_CONNECTION_TYPE: CONNECTION_TYPE_BLE}
    return flow


class TestAddressNormalization:
    """Address parsing shared with the Classic flow."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"),
            ("AA-BB-CC-DD-EE-FF", "AA:BB:CC:DD:EE:FF"),
            ("  AA:BB:CC:DD:EE:FF  ", "AA:BB:CC:DD:EE:FF"),
        ],
    )
    def test_normalizes_valid_addresses(self, raw, expected):
        assert _normalize_ble_address(raw) == expected

    @pytest.mark.parametrize("raw", ["", "not-an-address", "AA:BB:CC:DD:EE", None, 42])
    def test_returns_none_for_invalid(self, raw):
        assert _normalize_ble_address(raw) is None

    def test_unique_id_is_namespaced_apart_from_classic(self):
        """A dual-mode printer must be configurable over both transports."""
        assert _generate_ble_unique_id("AA:BB:CC:DD:EE:FF") == "ble:aa:bb:cc:dd:ee:ff"


class TestPrinterCandidateHeuristic:
    """Name matching that sorts likely printers into the filtered picker."""

    @pytest.mark.parametrize(
        "name",
        ["POS58 Printer", "Phomemo M02", "thermal-01", "GOOJPRT PT-210", "MTP-II", "GB01"],
    )
    def test_recognises_printer_names(self, name):
        assert _is_ble_printer_candidate({"name": name}) is True

    @pytest.mark.parametrize("name", ["Galaxy Buds", "Tile Tracker", "", None])
    def test_ignores_other_devices(self, name):
        assert _is_ble_printer_candidate({"name": name}) is False


class TestDeviceChoices:
    """Dropdown construction."""

    def test_filters_to_printer_like_devices(self, mock_ble_devices):
        mock_ble_devices.append(
            {
                "address": "99:99:99:99:99:99",
                "name": "Galaxy Buds",
                "label": "Galaxy Buds (99:99:99:99:99:99)",
                "rssi": -40,
                "_choice_key": "99:99:99:99:99:99",
            }
        )
        choices = _build_ble_device_choices(mock_ble_devices)
        assert "99:99:99:99:99:99" not in choices
        # ...but the user can always reach it.
        assert "__show_all__" in choices
        assert "__manual__" in choices

    def test_show_all_disables_the_filter(self, mock_ble_devices):
        mock_ble_devices.append(
            {
                "address": "99:99:99:99:99:99",
                "name": "Galaxy Buds",
                "label": "Galaxy Buds",
                "rssi": -40,
                "_choice_key": "99:99:99:99:99:99",
            }
        )
        choices = _build_ble_device_choices(mock_ble_devices, printers_only=False)
        assert "99:99:99:99:99:99" in choices
        # Nothing hidden any more, so no redundant "show all" entry.
        assert "__show_all__" not in choices

    def test_no_show_all_when_nothing_is_hidden(self, mock_ble_devices):
        choices = _build_ble_device_choices(mock_ble_devices)
        assert "__show_all__" not in choices

    def test_manual_entry_always_offered(self):
        assert "__manual__" in _build_ble_device_choices([])


class TestListBleDevices:
    """Reading HA's bluetooth state."""

    async def test_returns_empty_when_bluetooth_not_set_up(self, hass):
        assert await _list_ble_devices(hass) == []

    async def test_maps_and_sorts_by_signal_strength(self, hass):
        """Strongest first: the printer being set up is the one nearby."""
        hass.config.components.add("bluetooth")
        adverts = [
            FakeAdvert("11:22:33:44:55:66", "Far Printer", rssi=-90),
            FakeAdvert("AA:BB:CC:DD:EE:FF", "Near Printer", rssi=-40),
        ]
        with patch.object(ble_transport, "async_discovered_devices", return_value=adverts):
            devices = await _list_ble_devices(hass)

        assert [d["address"] for d in devices] == [
            "AA:BB:CC:DD:EE:FF",
            "11:22:33:44:55:66",
        ]
        assert devices[0]["label"] == "Near Printer (AA:BB:CC:DD:EE:FF) -40 dBm"

    async def test_skips_malformed_addresses(self, hass):
        hass.config.components.add("bluetooth")
        adverts = [FakeAdvert("garbage", "Bad"), FakeAdvert("AA:BB:CC:DD:EE:FF", "Good")]
        with patch.object(ble_transport, "async_discovered_devices", return_value=adverts):
            devices = await _list_ble_devices(hass)
        assert [d["address"] for d in devices] == ["AA:BB:CC:DD:EE:FF"]

    async def test_unnamed_device_falls_back_to_address(self, hass):
        hass.config.components.add("bluetooth")
        adverts = [FakeAdvert("AA:BB:CC:DD:EE:FF", "", rssi=None)]
        with patch.object(ble_transport, "async_discovered_devices", return_value=adverts):
            devices = await _list_ble_devices(hass)
        assert devices[0]["name"] == "AA:BB:CC:DD:EE:FF"
        assert devices[0]["label"] == "AA:BB:CC:DD:EE:FF"


class TestCanConnectBle:
    """The setup-time probe and its error mapping."""

    async def test_success_returns_resolved_uuid(self, hass):
        class Connection:
            characteristic_uuid = _FF02
            disconnected = False
            probe_writes: list = []

            async def async_write(self, data, chunk_delay_s):
                type(self).probe_writes.append(bytes(data))

            async def async_disconnect(self):
                type(self).disconnected = True

        with patch.object(ble_transport, "async_connect_ble", return_value=Connection()):
            ok, error, uuid = await _can_connect_ble(hass, "AA:BB:CC:DD:EE:FF", None, None)

        assert (ok, error, uuid) == (True, None, _FF02)
        # The probe writes ESC @ to prove the write path works, not just the
        # connect path -- a printer can accept the link and still reject data.
        assert Connection.probe_writes == [b"\x1b\x40"]
        # The probe must not hold a scarce proxy connection slot afterwards.
        assert Connection.disconnected is True

    @pytest.mark.parametrize(
        ("exc", "expected_code"),
        [
            (ble_transport.BluetoothUnavailableError("no stack"), "no_bluetooth"),
            (ble_transport.BleNotFoundError("out of range"), "not_found"),
            (BleWriteCharacteristicError("nothing writable"), "no_write_char"),
            (OSError("radio exploded"), "connect_failed"),
        ],
    )
    async def test_maps_failures_to_stable_codes(self, hass, exc, expected_code):
        # Pin a live scanner: with an empty pool every failure is correctly
        # reported as no_scanners instead, which the class below covers.
        with (
            patch.object(ble_transport, "async_connect_ble", side_effect=exc),
            patch.object(ble_transport, "connectable_scanner_count", return_value=1),
        ):
            ok, error, uuid = await _can_connect_ble(hass, "AA:BB:CC:DD:EE:FF", None, None)
        assert ok is False
        assert error == expected_code
        assert uuid is None

    @pytest.mark.parametrize(
        ("code", "key"),
        [
            ("not_found", "ble_not_found"),
            ("no_write_char", "ble_no_write_char"),
            ("connect_failed", "ble_connect_failed"),
            ("no_bluetooth", "ble_no_bluetooth"),
            (None, "cannot_connect_ble"),
            ("something_unknown", "cannot_connect_ble"),
        ],
    )
    def test_error_codes_map_to_translation_keys(self, code, key):
        assert _ble_error_to_key(code) == key


class TestWithResponseParsing:
    """The tri-state acknowledgement dropdown."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("auto", None),
            ("", None),
            (None, None),
            ("true", True),
            ("false", False),
            (True, True),
            (False, False),
        ],
    )
    def test_parses_dropdown_values(self, raw, expected):
        assert _parse_with_response(raw) is expected


class TestUserStepRouting:
    """Connection-type menu."""

    async def test_ble_choice_routes_to_ble_select(self, hass):
        flow = _flow(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
            return_value=[],
        ):
            result = await flow.async_step_user({CONF_CONNECTION_TYPE: CONNECTION_TYPE_BLE})
        # No devices visible -> guidance step.
        assert result["step_id"] == "ble_no_devices"

    async def test_menu_offers_ble_separately_from_classic(self, hass):
        flow = _flow(hass)
        result = await flow.async_step_user()
        key = next(k for k in result["data_schema"].schema if k == CONF_CONNECTION_TYPE)
        choices = result["data_schema"].schema[key].container
        assert CONNECTION_TYPE_BLE in choices
        assert "bluetooth" in choices
        # The labels must make the distinction obvious — proxies carry only BLE.
        assert "LE" in choices[CONNECTION_TYPE_BLE]
        assert "Classic" in choices["bluetooth"]


class TestBleSelectStep:
    """The discovered-device picker."""

    async def test_discovered_devices_shown(self, hass, mock_ble_devices):
        flow = _flow(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
            return_value=mock_ble_devices,
        ):
            result = await flow.async_step_ble_select()

        assert result["step_id"] == "ble_select"
        key = next(k for k in result["data_schema"].schema if k == CONF_BLE_DEVICE)
        choices = result["data_schema"].schema[key].container
        assert "AA:BB:CC:DD:EE:FF" in choices
        assert "11:22:33:44:55:66" in choices
        assert "__manual__" in choices

    async def test_preselects_suggested_profile_from_advertised_name(self, hass, mock_ble_devices):
        """ "Netum NT-1809DD" resolves through the alias table to NT-5890K."""
        flow = _flow(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
            return_value=mock_ble_devices,
        ):
            result = await flow.async_step_ble_select()

        profile_key = next(k for k in result["data_schema"].schema if k == CONF_PROFILE)
        assert profile_key.default() == "NT-5890K"

    async def test_generic_name_keeps_auto_profile(self, hass, mock_ble_devices):
        mock_ble_devices[0]["name"] = "BLE Printer"
        flow = _flow(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
            return_value=mock_ble_devices,
        ):
            result = await flow.async_step_ble_select()
        profile_key = next(k for k in result["data_schema"].schema if k == CONF_PROFILE)
        assert profile_key.default() == PROFILE_AUTO

    async def test_no_devices_routes_to_guidance(self, hass):
        flow = _flow(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
            return_value=[],
        ):
            result = await flow.async_step_ble_select()
        assert result["step_id"] == "ble_no_devices"

    async def test_show_all_re_renders_unfiltered(self, hass, mock_ble_devices):
        mock_ble_devices.append(
            {
                "address": "99:99:99:99:99:99",
                "name": "Galaxy Buds",
                "label": "Galaxy Buds (99:99:99:99:99:99)",
                "rssi": -40,
                "_choice_key": "99:99:99:99:99:99",
            }
        )
        flow = _flow(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
            return_value=mock_ble_devices,
        ):
            await flow.async_step_ble_select()
            result = await flow.async_step_ble_select({CONF_BLE_DEVICE: "__show_all__"})

        key = next(k for k in result["data_schema"].schema if k == CONF_BLE_DEVICE)
        assert "99:99:99:99:99:99" in result["data_schema"].schema[key].container

    async def test_manual_choice_routes_to_manual_step(self, hass, mock_ble_devices):
        flow = _flow(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
            return_value=mock_ble_devices,
        ):
            await flow.async_step_ble_select()
            result = await flow.async_step_ble_select({CONF_BLE_DEVICE: "__manual__"})
        assert result["step_id"] == "ble_manual"

    async def test_successful_selection_advances_to_codepage(self, hass, mock_ble_devices):
        flow = _flow(hass)
        with (
            patch(
                "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
                return_value=mock_ble_devices,
            ),
            patch(
                "custom_components.escpos_printer._config_flow.ble_steps._can_connect_ble",
                return_value=(True, None, _FF02),
            ),
        ):
            await flow.async_step_ble_select()
            result = await flow.async_step_ble_select(
                {
                    CONF_BLE_DEVICE: "AA:BB:CC:DD:EE:FF",
                    CONF_PROFILE: PROFILE_AUTO,
                    SECTION_BLE_ADVANCED: {},
                }
            )

        assert result["step_id"] == "codepage"
        assert flow._user_data[CONF_BLE_ADDRESS] == "AA:BB:CC:DD:EE:FF"
        # The probe's resolved UUID is persisted, so a firmware update that
        # reorders the service table cannot silently move the print target.
        assert flow._user_data[CONF_BLE_WRITE_UUID] == _FF02

    async def test_probe_failure_shows_the_mapped_error(self, hass, mock_ble_devices):
        flow = _flow(hass)
        with (
            patch(
                "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
                return_value=mock_ble_devices,
            ),
            patch(
                "custom_components.escpos_printer._config_flow.ble_steps._can_connect_ble",
                return_value=(False, "no_write_char", None),
            ),
        ):
            await flow.async_step_ble_select()
            result = await flow.async_step_ble_select(
                {
                    CONF_BLE_DEVICE: "AA:BB:CC:DD:EE:FF",
                    CONF_PROFILE: PROFILE_AUTO,
                    SECTION_BLE_ADVANCED: {},
                }
            )

        assert result["step_id"] == "ble_select"
        assert result["errors"]["base"] == "ble_no_write_char"

    async def test_invalid_override_uuid_is_rejected(self, hass, mock_ble_devices):
        flow = _flow(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
            return_value=mock_ble_devices,
        ):
            await flow.async_step_ble_select()
            result = await flow.async_step_ble_select(
                {
                    CONF_BLE_DEVICE: "AA:BB:CC:DD:EE:FF",
                    CONF_PROFILE: PROFILE_AUTO,
                    SECTION_BLE_ADVANCED: {CONF_BLE_WRITE_UUID: "nonsense"},
                }
            )
        assert result["errors"]["base"] == "invalid_ble_uuid"

    async def test_override_uuid_is_passed_to_the_probe(self, hass, mock_ble_devices):
        flow = _flow(hass)
        with (
            patch(
                "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
                return_value=mock_ble_devices,
            ),
            patch(
                "custom_components.escpos_printer._config_flow.ble_steps._can_connect_ble",
                return_value=(True, None, _2AF1),
            ) as probe,
        ):
            await flow.async_step_ble_select()
            await flow.async_step_ble_select(
                {
                    CONF_BLE_DEVICE: "AA:BB:CC:DD:EE:FF",
                    CONF_PROFILE: PROFILE_AUTO,
                    SECTION_BLE_ADVANCED: {
                        CONF_BLE_WRITE_UUID: "2af1",
                        CONF_BLE_WITH_RESPONSE: "false",
                    },
                }
            )
        # 16-bit shorthand is expanded before it reaches the probe.
        assert probe.call_args.args[2] == _2AF1
        assert probe.call_args.args[3] is False

    async def test_unknown_selection_is_rejected(self, hass, mock_ble_devices):
        flow = _flow(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
            return_value=mock_ble_devices,
        ):
            await flow.async_step_ble_select()
            result = await flow.async_step_ble_select(
                {CONF_BLE_DEVICE: "no-such-device", SECTION_BLE_ADVANCED: {}}
            )
        assert result["errors"]["base"] == "invalid_ble_address"


class TestBleManualStep:
    """Manual address entry."""

    async def test_form_offers_known_uuid_presets(self, hass):
        flow = _flow(hass)
        result = await flow.async_step_ble_manual()
        key = next(k for k in result["data_schema"].schema if k == CONF_BLE_WRITE_UUID)
        choices = result["data_schema"].schema[key].container
        assert "" in choices  # auto-detect
        assert _FF02 in choices

    async def test_invalid_address_rejected(self, hass):
        flow = _flow(hass)
        result = await flow.async_step_ble_manual({CONF_BLE_ADDRESS: "nope"})
        assert result["errors"]["base"] == "invalid_ble_address"

    async def test_valid_address_advances_to_codepage(self, hass):
        flow = _flow(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._can_connect_ble",
            return_value=(True, None, _FF02),
        ):
            result = await flow.async_step_ble_manual(
                {CONF_BLE_ADDRESS: "aa:bb:cc:dd:ee:ff", CONF_PROFILE: PROFILE_AUTO}
            )
        assert result["step_id"] == "codepage"
        assert flow._user_data[CONF_BLE_ADDRESS] == "AA:BB:CC:DD:EE:FF"

    async def test_no_devices_step_falls_through_to_manual(self, hass):
        flow = _flow(hass)
        result = await flow.async_step_ble_no_devices({})
        assert result["step_id"] == "ble_manual"

    async def test_no_devices_step_renders_guidance(self, hass):
        flow = _flow(hass)
        result = await flow.async_step_ble_no_devices()
        assert result["step_id"] == "ble_no_devices"
        assert "docs_url" in result["description_placeholders"]


class TestReconfigureBle:
    """Reconfiguring an existing BLE entry."""

    @staticmethod
    def _entry():
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        from custom_components.escpos_printer.const import CONF_TIMEOUT, DOMAIN

        return MockConfigEntry(
            domain=DOMAIN,
            title="BLE Printer AA:BB:CC:DD:EE:FF",
            data={
                CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
                CONF_BLE_WRITE_UUID: _FF02,
                CONF_TIMEOUT: 4.0,
                CONF_CONNECTION_TYPE: CONNECTION_TYPE_BLE,
            },
            unique_id="ble:aa:bb:cc:dd:ee:ff",
        )

    async def test_reconfigure_routes_to_the_ble_step(self, hass):
        entry = self._entry()
        entry.add_to_hass(hass)
        result = await entry.start_reconfigure_flow(hass)
        assert result["step_id"] == "reconfigure_ble"

    async def test_same_address_updates_the_entry(self, hass):
        entry = self._entry()
        entry.add_to_hass(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._can_connect_ble",
            return_value=(True, None, _2AF1),
        ):
            result = await entry.start_reconfigure_flow(hass)
            result2 = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_BLE_WRITE_UUID: ""},
            )

        assert result2["type"] == "abort"
        assert result2["reason"] == "reconfigure_successful"
        # Blank override means auto-detect, so the probe's resolution is stored.
        assert entry.data[CONF_BLE_WRITE_UUID] == _2AF1

    async def test_different_address_aborts(self, hass):
        """A BLE address is hardware identity — re-pointing is a different printer."""
        entry = self._entry()
        entry.add_to_hass(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._can_connect_ble",
            return_value=(True, None, _FF02),
        ) as probe:
            result = await entry.start_reconfigure_flow(hass)
            result2 = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_BLE_ADDRESS: "11:22:33:44:55:66"},
            )

        assert result2["type"] == "abort"
        assert result2["reason"] == "unique_id_mismatch"
        probe.assert_not_called()
        assert entry.data[CONF_BLE_ADDRESS] == "AA:BB:CC:DD:EE:FF"

    async def test_invalid_address_re_renders_with_error(self, hass):
        entry = self._entry()
        entry.add_to_hass(hass)
        result = await entry.start_reconfigure_flow(hass)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_BLE_ADDRESS: "nonsense"}
        )
        assert result2["step_id"] == "reconfigure_ble"
        assert result2["errors"]["base"] == "invalid_ble_address"

    async def test_invalid_uuid_re_renders_with_error(self, hass):
        entry = self._entry()
        entry.add_to_hass(hass)
        result = await entry.start_reconfigure_flow(hass)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_BLE_WRITE_UUID: "zzz"},
        )
        assert result2["errors"]["base"] == "invalid_ble_uuid"

    async def test_probe_failure_re_renders_with_error(self, hass):
        entry = self._entry()
        entry.add_to_hass(hass)
        with patch(
            "custom_components.escpos_printer._config_flow.ble_steps._can_connect_ble",
            return_value=(False, "not_found", None),
        ):
            result = await entry.start_reconfigure_flow(hass)
            result2 = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF"}
            )
        assert result2["errors"]["base"] == "ble_not_found"


class TestPairingProbe:
    """Setup-time detection of printers that require a bonded link."""

    async def test_probe_reports_needs_pairing_on_authorization_error(self, hass):
        """The whole point of writing during the probe rather than only connecting."""

        class Connection:
            characteristic_uuid = _FF02
            disconnected = False

            async def async_write(self, data, chunk_delay_s):
                raise ble_transport.BleAuthorizationError("Insufficient authorization (8)")

            async def async_disconnect(self):
                type(self).disconnected = True

        with patch.object(ble_transport, "async_connect_ble", return_value=Connection()):
            ok, error, _uuid = await _can_connect_ble(hass, "AA:BB:CC:DD:EE:FF", None, None)

        assert (ok, error) == (False, "needs_pairing")
        # Still released, even on the failure path.
        assert Connection.disconnected is True

    async def test_needs_pairing_maps_to_its_own_message(self):
        assert _ble_error_to_key("needs_pairing") == "ble_needs_pairing"

    async def test_pair_flag_reaches_the_connect_call(self, hass):
        class Connection:
            characteristic_uuid = _FF02

            async def async_write(self, data, chunk_delay_s):
                pass

            async def async_disconnect(self):
                pass

        with patch.object(ble_transport, "async_connect_ble", return_value=Connection()) as connect:
            await _can_connect_ble(hass, "AA:BB:CC:DD:EE:FF", None, None, True)
        assert connect.call_args.kwargs["pair"] is True

    async def test_pairing_choice_is_stored_on_the_entry(self, hass, mock_ble_devices):
        flow = _flow(hass)
        with (
            patch(
                "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
                return_value=mock_ble_devices,
            ),
            patch(
                "custom_components.escpos_printer._config_flow.ble_steps._can_connect_ble",
                return_value=(True, None, _FF02),
            ) as probe,
        ):
            await flow.async_step_ble_select()
            await flow.async_step_ble_select(
                {
                    CONF_BLE_DEVICE: "AA:BB:CC:DD:EE:FF",
                    CONF_PROFILE: PROFILE_AUTO,
                    SECTION_BLE_ADVANCED: {CONF_BLE_PAIR: True},
                }
            )
        assert probe.call_args.args[4] is True
        assert flow._user_data[CONF_BLE_PAIR] is True

    async def test_pairing_defaults_off(self, hass, mock_ble_devices):
        flow = _flow(hass)
        with (
            patch(
                "custom_components.escpos_printer._config_flow.ble_steps._list_ble_devices",
                return_value=mock_ble_devices,
            ),
            patch(
                "custom_components.escpos_printer._config_flow.ble_steps._can_connect_ble",
                return_value=(True, None, _FF02),
            ),
        ):
            await flow.async_step_ble_select()
            await flow.async_step_ble_select(
                {
                    CONF_BLE_DEVICE: "AA:BB:CC:DD:EE:FF",
                    CONF_PROFILE: PROFILE_AUTO,
                    SECTION_BLE_ADVANCED: {},
                }
            )
        assert flow._user_data[CONF_BLE_PAIR] is False


class TestEmptyScannerPool:
    """An infrastructure outage must not be reported as a printer problem.

    Home Assistant caches discovered devices, so after every Bluetooth proxy
    goes offline a stale handle still resolves and the failure only appears
    at connect time. Blaming range or connection slots there sends the user
    hunting in entirely the wrong place -- which is exactly what happened
    when all three of a real user's ESPHome proxies dropped off the network.
    """

    @pytest.mark.parametrize(
        "exc",
        [
            OSError("Could not connect"),
            ble_transport.BleNotFoundError("out of range"),
        ],
    )
    async def test_no_scanners_beats_printer_specific_codes(self, hass, exc):
        with (
            patch.object(ble_transport, "async_connect_ble", side_effect=exc),
            patch.object(ble_transport, "connectable_scanner_count", return_value=0),
        ):
            ok, error, _uuid = await _can_connect_ble(hass, "AA:BB:CC:DD:EE:FF", None, None)
        assert (ok, error) == (False, "no_scanners")

    async def test_no_scanners_maps_to_its_own_message(self):
        assert _ble_error_to_key("no_scanners") == "ble_no_scanners"

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ble_transport.BluetoothUnavailableError("no stack"), "no_bluetooth"),
            (BleWriteCharacteristicError("nothing writable"), "no_write_char"),
            (ble_transport.BleAuthorizationError("auth"), "needs_pairing"),
        ],
    )
    async def test_definitive_diagnoses_survive_an_empty_pool(self, hass, exc, expected):
        """These say something true about the device regardless of scanners.

        We only reached them by talking to the printer, so an empty pool
        afterwards must not overwrite a diagnosis we actually earned.
        """
        with (
            patch.object(ble_transport, "async_connect_ble", side_effect=exc),
            patch.object(ble_transport, "connectable_scanner_count", return_value=0),
        ):
            ok, error, _uuid = await _can_connect_ble(hass, "AA:BB:CC:DD:EE:FF", None, None)
        assert (ok, error) == (False, expected)


class TestConnectableScannerCount:
    """The seam that reports how many adapters/proxies are live."""

    def test_zero_when_bluetooth_not_set_up(self, hass):
        assert ble_transport.connectable_scanner_count(hass) == 0

    def test_reads_the_bluetooth_manager_when_available(self, hass, monkeypatch):
        import sys
        import types

        from homeassistant import components

        module = types.ModuleType("homeassistant.components.bluetooth")
        captured = {}

        def _count(_hass, connectable=False):
            captured["connectable"] = connectable
            return 3

        module.async_scanner_count = _count
        monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", module)
        monkeypatch.setattr(components, "bluetooth", module, raising=False)
        hass.config.components.add("bluetooth")

        assert ble_transport.connectable_scanner_count(hass) == 3
        # Passive-only scanners can't print, so they must not count.
        assert captured["connectable"] is True
