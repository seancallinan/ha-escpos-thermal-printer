"""BLE (GATT) configuration steps mixin.

Mirrors ``bluetooth_steps`` in shape, but the discovery source is Home
Assistant's ``bluetooth`` integration rather than bluez D-Bus — which is
what lets this flow configure a printer that only an ESPHome Bluetooth
proxy can reach.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.data_entry_flow import section
from homeassistant.exceptions import HomeAssistantError
import voluptuous as vol

from ..capabilities import (
    PROFILE_AUTO,
    PROFILE_CUSTOM,
    get_profile_choices_dict,
    suggest_profile,
)
from ..const import (
    BLE_MANUAL_ENTRY_KEY,
    BLE_SHOW_ALL_KEY,
    CONF_BLE_ADDRESS,
    CONF_BLE_DEVICE,
    CONF_BLE_WITH_RESPONSE,
    CONF_BLE_WRITE_UUID,
    CONF_CONNECTION_TYPE,
    CONF_PROFILE,
    CONF_TIMEOUT,
    CONNECTION_TYPE_BLE,
    DEFAULT_TIMEOUT,
)
from ..security import sanitize_log_message, validate_ble_uuid
from .ble_helpers import (
    _ble_error_to_key,
    _build_ble_device_choices,
    _can_connect_ble,
    _generate_ble_unique_id,
    _is_ble_printer_candidate,
    _known_uuid_choices,
    _list_ble_devices,
    _normalize_ble_address,
)

_LOGGER = logging.getLogger(__name__)

# Schema key of the collapsed section holding the GATT overrides on the
# ble_select form. Mirrored in strings.json / translations under
# config.step.ble_select.sections.
SECTION_BLE_ADVANCED = "advanced_options"

# Sentinel for "let the characteristic decide" on the with-response dropdown.
_RESPONSE_AUTO = "auto"
_RESPONSE_CHOICES: dict[str, str] = {
    _RESPONSE_AUTO: "Auto (recommended) — acknowledged when supported",
    "true": "Always acknowledged (slower, more reliable)",
    "false": "Never acknowledged (faster, can drop bytes)",
}


def _parse_with_response(raw: Any) -> bool | None:
    """Map the with-response dropdown value to the config tri-state."""
    if raw in (None, "", _RESPONSE_AUTO):
        return None
    if isinstance(raw, bool):
        return raw
    return str(raw).lower() == "true"


async def _suggest_ble_default_profile(
    hass: Any,
    devices: list[dict[str, Any]],
    default_key: str,
    profile_choices: dict[str, str],
) -> str:
    """Suggest a profile from the default device's advertised BLE name.

    Same preselect-only pattern as the USB and Classic flows: portable
    printers often advertise their model, generic names yield no suggestion
    and the dropdown default stays on auto.
    """
    device = next((d for d in devices if d.get("_choice_key") == default_key), None)
    if device and device.get("name"):
        suggestion = await hass.async_add_executor_job(suggest_profile, device["name"], None, None)
        if suggestion and suggestion in profile_choices:
            return str(suggestion)
    return PROFILE_AUTO


class BleFlowMixin:
    """Mixin providing BLE (GATT) configuration steps.

    Expects to be mixed into a class providing ``hass``, ``_user_data``,
    ``_discovered_ble_devices``, ``_show_all_ble_devices``, the standard
    ConfigFlow helpers, and the shared ``async_step_codepage`` /
    ``async_step_custom_profile`` continuation steps.
    """

    hass: Any
    _user_data: dict[str, Any]
    _discovered_ble_devices: list[dict[str, Any]]
    _show_all_ble_devices: bool

    async def async_step_ble_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle BLE printer selection from discovered devices."""
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug("Config flow BLE select input keys: %s", sorted(user_input.keys()))
            selected = user_input.get(CONF_BLE_DEVICE)
            if selected == BLE_MANUAL_ENTRY_KEY:
                return await self.async_step_ble_manual()
            if selected == BLE_SHOW_ALL_KEY:
                self._show_all_ble_devices = True
                return await self.async_step_ble_select()

            chosen = next(
                (d for d in self._discovered_ble_devices if d.get("_choice_key") == selected),
                None,
            )
            if chosen is None:
                errors["base"] = "invalid_ble_address"
            else:
                advanced = user_input.get(SECTION_BLE_ADVANCED) or {}
                write_uuid, with_response, uuid_error = self._read_gatt_overrides(
                    advanced, user_input
                )
                if uuid_error:
                    errors["base"] = uuid_error

            if not errors:
                assert chosen is not None  # narrowed by errors check above
                result = await self._finalize_ble_step(
                    address=chosen["address"],
                    write_uuid=write_uuid,
                    with_response=with_response,
                    timeout=float(user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
                    profile=user_input.get(CONF_PROFILE, PROFILE_AUTO),
                    printer_name=chosen.get("name") or f"BLE Printer {chosen['address']}",
                    errors=errors,
                )
                if result is not None:
                    return result

        # Discover only on the first render; re-renders after a validation
        # error reuse the cached list so a retry doesn't reshuffle the
        # dropdown under the user (RSSI ordering makes it jumpy otherwise).
        if not self._discovered_ble_devices:
            self._discovered_ble_devices = await _list_ble_devices(self.hass)
        if not self._discovered_ble_devices:
            return await self.async_step_ble_no_devices()

        printers_only = not self._show_all_ble_devices
        if printers_only and not any(
            _is_ble_printer_candidate(d) for d in self._discovered_ble_devices
        ):
            printers_only = False
        device_choices = _build_ble_device_choices(
            self._discovered_ble_devices, printers_only=printers_only
        )
        profile_choices = await self.hass.async_add_executor_job(get_profile_choices_dict)
        default_device = next(iter(device_choices.keys()))
        default_profile = await _suggest_ble_default_profile(
            self.hass, self._discovered_ble_devices, default_device, profile_choices
        )

        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_BLE_DEVICE, default=default_device): vol.In(device_choices),
            vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): vol.Coerce(float),
            vol.Optional(CONF_PROFILE, default=default_profile): vol.In(profile_choices),
            vol.Required(SECTION_BLE_ADVANCED): section(
                vol.Schema(
                    {
                        vol.Optional(CONF_BLE_WRITE_UUID, default=""): str,
                        vol.Optional(CONF_BLE_WITH_RESPONSE, default=_RESPONSE_AUTO): vol.In(
                            _RESPONSE_CHOICES
                        ),
                    }
                ),
                {"collapsed": True},
            ),
        }
        return self.async_show_form(  # type: ignore[attr-defined,no-any-return]
            step_id="ble_select", data_schema=vol.Schema(schema_dict), errors=errors
        )

    async def async_step_ble_no_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Guidance step when HA's bluetooth stack sees no connectable devices.

        Reached when the bluetooth integration isn't set up, no adapter or
        proxy is configured, or every visible scanner is passive-only. Offers
        a path to manual entry for users who already know the address.
        """
        if user_input is not None:
            return await self.async_step_ble_manual()

        return self.async_show_form(  # type: ignore[attr-defined,no-any-return]
            step_id="ble_no_devices",
            data_schema=vol.Schema({}),
            description_placeholders={
                "docs_url": (
                    "https://github.com/cognitivegears/ha-escpos-thermal-printer"
                    "/blob/main/docs/ble.md"
                )
            },
        )

    async def async_step_ble_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual BLE address entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug("Config flow BLE manual input keys: %s", sorted(user_input.keys()))
            address = _normalize_ble_address(str(user_input.get(CONF_BLE_ADDRESS, "")).strip())
            if address is None:
                errors["base"] = "invalid_ble_address"

            write_uuid, with_response, uuid_error = self._read_gatt_overrides({}, user_input)
            if uuid_error:
                errors["base"] = uuid_error

            if not errors:
                assert address is not None  # narrowed by errors check
                result = await self._finalize_ble_step(
                    address=address,
                    write_uuid=write_uuid,
                    with_response=with_response,
                    timeout=float(user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
                    profile=user_input.get(CONF_PROFILE, PROFILE_AUTO),
                    printer_name=f"BLE Printer {address}",
                    errors=errors,
                )
                if result is not None:
                    return result

        profile_choices = await self.hass.async_add_executor_job(get_profile_choices_dict)
        # Manual entry keeps the GATT overrides visible: a user typing a raw
        # address is likely the one whose printer auto-detection missed.
        data_schema = vol.Schema(
            {
                vol.Required(CONF_BLE_ADDRESS): str,
                vol.Optional(CONF_BLE_WRITE_UUID, default=""): vol.In(_known_uuid_choices()),
                vol.Optional(CONF_BLE_WITH_RESPONSE, default=_RESPONSE_AUTO): vol.In(
                    _RESPONSE_CHOICES
                ),
                vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): vol.Coerce(float),
                vol.Optional(CONF_PROFILE, default=PROFILE_AUTO): vol.In(profile_choices),
            }
        )
        return self.async_show_form(  # type: ignore[attr-defined,no-any-return]
            step_id="ble_manual", data_schema=data_schema, errors=errors
        )

    async def async_step_reconfigure_ble(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of an existing BLE printer entry.

        As with the Classic flow, a BLE address is the printer's hardware
        identity rather than a lookup address that can drift, so re-pointing
        an entry at a different address is treated as configuring a different
        printer and aborts via the unique-ID mismatch guard.
        """
        reconfigure_entry = self._get_reconfigure_entry()  # type: ignore[attr-defined]
        errors: dict[str, str] = {}

        if user_input is not None:
            address = _normalize_ble_address(str(user_input.get(CONF_BLE_ADDRESS, "")).strip())
            if address is None:
                errors["base"] = "invalid_ble_address"

            write_uuid, with_response, uuid_error = self._read_gatt_overrides({}, user_input)
            if uuid_error:
                errors["base"] = uuid_error

            if not errors:
                assert address is not None  # narrowed by errors check above
                timeout = float(user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))

                unique_id = _generate_ble_unique_id(address)
                await self.async_set_unique_id(unique_id)  # type: ignore[attr-defined]
                self._abort_if_unique_id_mismatch()  # type: ignore[attr-defined]

                ok, error_code, resolved_uuid = await _can_connect_ble(
                    self.hass, address, write_uuid, with_response
                )
                if ok:
                    return self.async_update_reload_and_abort(  # type: ignore[attr-defined,no-any-return]
                        reconfigure_entry,
                        unique_id=unique_id,
                        data_updates={
                            CONF_BLE_ADDRESS: address,
                            CONF_BLE_WRITE_UUID: write_uuid or resolved_uuid,
                            CONF_BLE_WITH_RESPONSE: with_response,
                            CONF_TIMEOUT: timeout,
                        },
                    )
                errors["base"] = _ble_error_to_key(error_code)

        data_schema = vol.Schema(
            {
                vol.Required(CONF_BLE_ADDRESS): str,
                vol.Optional(CONF_BLE_WRITE_UUID, default=""): str,
                vol.Optional(CONF_BLE_WITH_RESPONSE, default=_RESPONSE_AUTO): vol.In(
                    _RESPONSE_CHOICES
                ),
                vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): vol.Coerce(float),
            }
        )
        return self.async_show_form(  # type: ignore[attr-defined,no-any-return]
            step_id="reconfigure_ble",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                data_schema, user_input or reconfigure_entry.data
            ),
            errors=errors,
        )

    def _read_gatt_overrides(
        self, section_input: dict[str, Any], user_input: dict[str, Any]
    ) -> tuple[str | None, bool | None, str | None]:
        """Pull the write-UUID / with-response overrides out of a form payload.

        Returns ``(write_uuid, with_response, error_key)``. The UUID arrives
        either nested under the collapsed advanced section or flat (manual
        entry, and programmatic callers that bypass the schema), so both are
        accepted.
        """
        raw_uuid = str(
            section_input.get(
                CONF_BLE_WRITE_UUID,
                user_input.get(CONF_BLE_WRITE_UUID, ""),
            )
            or ""
        ).strip()
        raw_response = section_input.get(
            CONF_BLE_WITH_RESPONSE,
            user_input.get(CONF_BLE_WITH_RESPONSE, _RESPONSE_AUTO),
        )

        write_uuid: str | None = None
        if raw_uuid:
            try:
                write_uuid = validate_ble_uuid(raw_uuid)
            except HomeAssistantError:
                return None, None, "invalid_ble_uuid"
        return write_uuid, _parse_with_response(raw_response), None

    async def _finalize_ble_step(
        self,
        *,
        address: str,
        write_uuid: str | None,
        with_response: bool | None,
        timeout: float,
        profile: str,
        printer_name: str,
        errors: dict[str, str],
    ) -> ConfigFlowResult | None:
        """Set unique ID, run the connect probe, branch to the next step.

        Returns a ConfigFlowResult on success (caller returns it directly).
        On failure, mutates ``errors["base"]`` and returns ``None`` so the
        caller re-renders its own form with the error.
        """
        await self.async_set_unique_id(_generate_ble_unique_id(address))  # type: ignore[attr-defined]
        self._abort_if_unique_id_configured()  # type: ignore[attr-defined]

        _LOGGER.debug("Attempting BLE connection test to %s", sanitize_log_message(address))
        ok, error_code, resolved_uuid = await _can_connect_ble(
            self.hass, address, write_uuid, with_response
        )
        if ok:
            self._user_data = {
                CONF_CONNECTION_TYPE: CONNECTION_TYPE_BLE,
                CONF_BLE_ADDRESS: address,
                # Persist what the probe actually resolved, so a later
                # firmware update that reorders the service table cannot
                # silently move where we print.
                CONF_BLE_WRITE_UUID: write_uuid or resolved_uuid,
                CONF_BLE_WITH_RESPONSE: with_response,
                CONF_TIMEOUT: timeout,
                CONF_PROFILE: profile,
                "_printer_name": printer_name,
            }
            if profile == PROFILE_CUSTOM:
                return await self.async_step_custom_profile()  # type: ignore[attr-defined,no-any-return]
            return await self.async_step_codepage()  # type: ignore[attr-defined,no-any-return]

        _LOGGER.warning(
            "BLE connection test failed for %s: %s",
            sanitize_log_message(address),
            error_code,
        )
        errors["base"] = _ble_error_to_key(error_code)
        return None
