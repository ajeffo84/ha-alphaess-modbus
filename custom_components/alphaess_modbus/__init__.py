from __future__ import annotations

import re

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .config_flow import CONF_SINGLE_WRITE_FC16, CONF_SLAVE_ID
from .const import DOMAIN, PLATFORMS
from .coordinator import AlphaESSCoordinator
from .modbus_client import AlphaESSModbusClient

SERVICE_WRITE_REGISTER = "write_register"
SERVICE_WRITE_REGISTERS = "write_registers"
_ADDR = vol.All(vol.Coerce(int), vol.Range(min=0, max=65535))
_WORD = vol.All(vol.Coerce(int), vol.Range(min=0, max=65535))
SERVICE_WRITE_REGISTER_SCHEMA = vol.Schema({
    vol.Required("address"): _ADDR,
    vol.Required("value"): _WORD,
    # Omitted = follow the config-entry "single_write_fc16" option.
    vol.Optional("use_fc16"): bool,
    vol.Optional("verify", default=False): bool,
})
SERVICE_WRITE_REGISTERS_SCHEMA = vol.Schema({
    vol.Required("address"): _ADDR,
    vol.Required("values"): vol.All([_WORD], vol.Length(min=1, max=123)),
    vol.Optional("verify", default=False): bool,
})

_MODEL_PREFIXES: dict[str, str] = {
    "SMILE-T10": "SMILE-T10",
    "SMILE-M5": "SMILE-M5",
    "SMILE-B3": "SMILE-B3",
    "SMILE-S5": "SMILE-S5",
    "SMILE5": "SMILE5",
    "STORION": "STORION",
    "ALD": "Alphastore",
}


def _detect_model(raw_sn: str) -> str:
    if not raw_sn:
        return "SMILE series"
    sn_upper = raw_sn.upper()
    for prefix in sorted(_MODEL_PREFIXES, key=len, reverse=True):
        if sn_upper.startswith(prefix):
            return _MODEL_PREFIXES[prefix]
    _match = re.match(r"^(.+?)(?:20\d{2}|19\d{2})", raw_sn)
    if _match:
        return _match.group(1).rstrip("-_ ").strip() or "SMILE series"
    return raw_sn[:20].strip() or "SMILE series"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from homeassistant.exceptions import ConfigEntryNotReady

    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    client = AlphaESSModbusClient(
        host=host,
        port=port,
        slave_id=entry.data[CONF_SLAVE_ID],
        single_write_fc16=entry.options.get(CONF_SINGLE_WRITE_FC16, False),
    )
    try:
        await client.connect()
    except Exception as err:
        raise ConfigEntryNotReady(f"Cannot connect to {host}:{port}: {err}") from err
    if not client.connected:
        raise ConfigEntryNotReady(f"Cannot connect to {host}:{port}")

    coordinator = AlphaESSCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    raw_sn = (coordinator.data.get("inverter_sn") or "").strip().rstrip("\x00").strip()
    _model = _detect_model(raw_sn)
    _serial = raw_sn or None

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    if not hass.services.has_service(DOMAIN, SERVICE_WRITE_REGISTER):
        def _coordinators() -> list[AlphaESSCoordinator]:
            # hass.data[DOMAIN] also holds non-coordinator entries (e.g. the
            # per-entry switches map), so only return coordinators.
            return [
                o for o in hass.data[DOMAIN].values()
                if isinstance(o, AlphaESSCoordinator)
            ]

        async def _handle_write_register(call: ServiceCall) -> ServiceResponse:
            address: int = call.data["address"]
            value: int = call.data["value"]
            verify: bool = call.data["verify"]
            readbacks = []
            for obj in _coordinators():
                rb = await obj.async_write_raw(
                    address, value, use_fc16=call.data.get("use_fc16"), verify=verify
                )
                if verify and rb != value:
                    raise HomeAssistantError(
                        f"Register {address:#06x}: wrote {value}, read back {rb}"
                    )
                readbacks.append(rb)
            return {"address": address, "readback": readbacks}

        async def _handle_write_registers(call: ServiceCall) -> ServiceResponse:
            address: int = call.data["address"]
            values: list[int] = call.data["values"]
            verify: bool = call.data["verify"]
            readbacks = []
            for obj in _coordinators():
                rb = await obj.async_write_registers(address, values, verify=verify)
                if verify and rb != values:
                    raise HomeAssistantError(
                        f"Registers {address:#06x}+{len(values)}: wrote {values}, "
                        f"read back {rb}"
                    )
                readbacks.append(rb)
            return {"address": address, "readback": readbacks}

        hass.services.async_register(
            DOMAIN,
            SERVICE_WRITE_REGISTER,
            _handle_write_register,
            schema=SERVICE_WRITE_REGISTER_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_WRITE_REGISTERS,
            _handle_write_registers,
            schema=SERVICE_WRITE_REGISTERS_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="AlphaESS Inverter",
        manufacturer="AlphaESS",
        model=_model,
        serial_number=_serial,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: AlphaESSCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        hass.data[DOMAIN].pop(f"{entry.entry_id}_switches", None)
        await coordinator.client.close()
        if not any(isinstance(o, AlphaESSCoordinator) for o in hass.data[DOMAIN].values()):
            hass.services.async_remove(DOMAIN, SERVICE_WRITE_REGISTER)
            hass.services.async_remove(DOMAIN, SERVICE_WRITE_REGISTERS)
    return unloaded
