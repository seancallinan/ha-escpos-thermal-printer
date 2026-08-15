"""Tests for BLE GATT characteristic selection and chunk splitting."""

import pytest

from custom_components.escpos_printer.printer.ble_gatt import (
    KNOWN_WRITE_UUIDS,
    BleWriteCharacteristicError,
    describe_services,
    is_writable,
    max_write_size,
    prefers_response,
    select_write_characteristic,
)
from custom_components.escpos_printer.printer.transport_utils import iter_chunks

_FF02 = KNOWN_WRITE_UUIDS[0]
_2AF1 = KNOWN_WRITE_UUIDS[1]
_NORDIC = KNOWN_WRITE_UUIDS[2]


class FakeCharacteristic:
    """Stand-in for bleak's BleakGATTCharacteristic."""

    def __init__(self, uuid, properties, handle=1, max_write_without_response_size=0):
        self.uuid = uuid
        self.properties = list(properties)
        self.handle = handle
        self.max_write_without_response_size = max_write_without_response_size


class FakeService:
    """Stand-in for bleak's BleakGATTService."""

    def __init__(self, uuid, characteristics):
        self.uuid = uuid
        self.characteristics = characteristics


class FakeClient:
    """Stand-in for a connected BleakClient."""

    def __init__(self, services, mtu_size=0):
        self.services = services
        self.mtu_size = mtu_size


def _client(*characteristics, mtu_size=0):
    return FakeClient([FakeService("svc", list(characteristics))], mtu_size=mtu_size)


class TestSelectWriteCharacteristic:
    """Resolution order for the ESC/POS write channel."""

    def test_prefers_known_uuid_over_discovery_order(self):
        """A known printer UUID wins even when another writable comes first.

        This is the whole point of the preference list: printers commonly
        expose a vendor control characteristic ahead of the data channel, and
        picking the first writable would print into a void.
        """
        other = FakeCharacteristic("0000abcd-0000-1000-8000-00805f9b34fb", ["write"])
        printer = FakeCharacteristic(_FF02, ["write-without-response"])
        assert select_write_characteristic(_client(other, printer)) is printer

    def test_known_uuid_order_is_honoured(self):
        """Earlier entries in KNOWN_WRITE_UUIDS beat later ones."""
        nordic = FakeCharacteristic(_NORDIC, ["write"])
        ff02 = FakeCharacteristic(_FF02, ["write"])
        # Discovery order deliberately puts the lower-priority one first.
        assert select_write_characteristic(_client(nordic, ff02)) is ff02

    def test_falls_back_to_first_writable(self):
        unknown = FakeCharacteristic("0000abcd-0000-1000-8000-00805f9b34fb", ["write"])
        assert select_write_characteristic(_client(unknown)) is unknown

    def test_skips_non_writable_characteristics(self):
        read_only = FakeCharacteristic("0000aaaa-0000-1000-8000-00805f9b34fb", ["read", "notify"])
        writable = FakeCharacteristic("0000bbbb-0000-1000-8000-00805f9b34fb", ["write"])
        assert select_write_characteristic(_client(read_only, writable)) is writable

    def test_known_uuid_that_is_not_writable_is_skipped(self):
        """A known UUID exposed read-only must not shadow a real write channel."""
        fake_ff02 = FakeCharacteristic(_FF02, ["read"])
        real = FakeCharacteristic("0000bbbb-0000-1000-8000-00805f9b34fb", ["write"])
        assert select_write_characteristic(_client(fake_ff02, real)) is real

    def test_no_writable_characteristic_raises(self):
        read_only = FakeCharacteristic("0000aaaa-0000-1000-8000-00805f9b34fb", ["read"])
        with pytest.raises(BleWriteCharacteristicError, match="no writable"):
            select_write_characteristic(_client(read_only))

    def test_empty_service_table_raises(self):
        with pytest.raises(BleWriteCharacteristicError):
            select_write_characteristic(FakeClient([]))


class TestSelectWriteCharacteristicOverride:
    """An explicit override is honoured exactly, never silently ignored."""

    def test_override_wins_over_known_uuid(self):
        known = FakeCharacteristic(_FF02, ["write"])
        override = FakeCharacteristic(_2AF1, ["write"])
        chosen = select_write_characteristic(_client(known, override), _2AF1)
        assert chosen is override

    def test_override_accepts_16_bit_shorthand(self):
        """Users quote these as 'ff02'; bleak reports the 128-bit form."""
        target = FakeCharacteristic(_FF02, ["write"])
        assert select_write_characteristic(_client(target), "ff02") is target

    def test_override_is_case_insensitive(self):
        target = FakeCharacteristic(_FF02, ["write"])
        assert select_write_characteristic(_client(target), _FF02.upper()) is target

    def test_missing_override_raises_rather_than_falling_back(self):
        """The fallback must not fire — a wrong UUID is a user error to report."""
        other = FakeCharacteristic(_FF02, ["write"])
        with pytest.raises(BleWriteCharacteristicError, match="not found"):
            select_write_characteristic(_client(other), _2AF1)

    def test_non_writable_override_raises(self):
        target = FakeCharacteristic(_FF02, ["read", "notify"])
        with pytest.raises(BleWriteCharacteristicError, match="not writable"):
            select_write_characteristic(_client(target), _FF02)

    def test_invalid_override_uuid_raises(self):
        from homeassistant.exceptions import HomeAssistantError

        target = FakeCharacteristic(_FF02, ["write"])
        with pytest.raises(HomeAssistantError):
            select_write_characteristic(_client(target), "not-a-uuid")


class TestWriteProperties:
    """is_writable / prefers_response property mapping."""

    @pytest.mark.parametrize(
        ("properties", "expected"),
        [
            (["write"], True),
            (["write-without-response"], True),
            (["write", "read"], True),
            (["read"], False),
            (["notify", "indicate"], False),
            ([], False),
        ],
    )
    def test_is_writable(self, properties, expected):
        assert is_writable(FakeCharacteristic("u", properties)) is expected

    def test_prefers_response_when_acknowledged_write_supported(self):
        assert prefers_response(FakeCharacteristic("u", ["write"])) is True

    def test_no_response_preference_without_acknowledged_write(self):
        assert prefers_response(FakeCharacteristic("u", ["write-without-response"])) is False


class TestMaxWriteSize:
    """Chunk sizing against negotiated MTU."""

    def test_without_response_uses_bleak_reported_size(self):
        char = FakeCharacteristic(
            _FF02, ["write-without-response"], max_write_without_response_size=244
        )
        assert max_write_size(_client(char, mtu_size=247), char, with_response=False) == 244

    def test_with_response_uses_mtu_minus_att_overhead(self):
        char = FakeCharacteristic(_FF02, ["write"], max_write_without_response_size=244)
        assert max_write_size(_client(char, mtu_size=247), char, with_response=True) == 244

    def test_falls_back_to_ble_default_payload(self):
        """An un-negotiated link carries 20 bytes; never guess higher."""
        char = FakeCharacteristic(_FF02, ["write"])
        assert max_write_size(_client(char, mtu_size=0), char, with_response=True) == 20

    def test_zero_reported_without_response_size_falls_back_to_mtu(self):
        char = FakeCharacteristic(_FF02, ["write-without-response"])
        assert max_write_size(_client(char, mtu_size=100), char, with_response=False) == 97


class TestDescribeServices:
    """The diagnostics dump users read to find their write UUID."""

    def test_flattens_and_flags_known_uuids(self):
        char = FakeCharacteristic(_FF02, ["write", "read"], handle=7)
        other = FakeCharacteristic("0000aaaa-0000-1000-8000-00805f9b34fb", ["read"], handle=9)
        rows = describe_services(FakeClient([FakeService("svc-1", [char, other])]))

        assert rows == [
            {
                "service_uuid": "svc-1",
                "characteristic_uuid": _FF02,
                "handle": 7,
                "properties": ["read", "write"],
                "writable": True,
                "known_printer_uuid": True,
            },
            {
                "service_uuid": "svc-1",
                "characteristic_uuid": "0000aaaa-0000-1000-8000-00805f9b34fb",
                "handle": 9,
                "properties": ["read"],
                "writable": False,
                "known_printer_uuid": False,
            },
        ]


class TestIterChunks:
    """Splitting rule shared by the serial and BLE transports."""

    def test_empty_payload_yields_nothing(self):
        assert list(iter_chunks(b"", 4)) == []

    def test_payload_smaller_than_chunk_is_one_final_chunk(self):
        assert list(iter_chunks(b"abc", 10)) == [(b"abc", True)]

    def test_zero_chunk_size_disables_splitting(self):
        assert list(iter_chunks(b"abcdef", 0)) == [(b"abcdef", True)]

    def test_negative_chunk_size_disables_splitting(self):
        assert list(iter_chunks(b"abcdef", -1)) == [(b"abcdef", True)]

    def test_exact_multiple_flags_only_the_last_chunk(self):
        """The boundary case: a final chunk exactly filling chunk_size."""
        assert list(iter_chunks(b"abcdef", 2)) == [
            (b"ab", False),
            (b"cd", False),
            (b"ef", True),
        ]

    def test_ragged_final_chunk(self):
        assert list(iter_chunks(b"abcde", 2)) == [
            (b"ab", False),
            (b"cd", False),
            (b"e", True),
        ]

    def test_payload_equal_to_chunk_size_is_single_final_chunk(self):
        assert list(iter_chunks(b"abcd", 4)) == [(b"abcd", True)]

    def test_exactly_one_chunk_flagged_last(self):
        """Regression: an is_last flag computed per-index must not miss."""
        chunks = list(iter_chunks(b"x" * 9, 3))
        assert [is_last for _, is_last in chunks] == [False, False, True]
        assert b"".join(chunk for chunk, _ in chunks) == b"x" * 9
