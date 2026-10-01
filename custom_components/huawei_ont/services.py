"""Services for Huawei ONT: DHCP static IP reservations."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import HuaweiOntCoordinator

SERVICE_SET_STATIC_IP = "set_static_ip"
SERVICE_CLEAR_STATIC_IP = "clear_static_ip"
ATTR_MAC_ADDRESS = "mac_address"
ATTR_IP_ADDRESS = "ip_address"

_TARGET = {
    vol.Exclusive(ATTR_ENTITY_ID, "target"): cv.entity_id,
    vol.Exclusive(ATTR_MAC_ADDRESS, "target"): cv.string,
}
SET_SCHEMA = vol.All(
    vol.Schema({**_TARGET, vol.Optional(ATTR_IP_ADDRESS): cv.string}),
    cv.has_at_least_one_key(ATTR_ENTITY_ID, ATTR_MAC_ADDRESS),
)
CLEAR_SCHEMA = vol.All(
    vol.Schema(_TARGET),
    cv.has_at_least_one_key(ATTR_ENTITY_ID, ATTR_MAC_ADDRESS),
)


def _coordinator(hass: HomeAssistant) -> HuaweiOntCoordinator:
    coordinators = list(hass.data.get(DOMAIN, {}).values())
    if not coordinators:
        raise HomeAssistantError("Huawei ONT is not set up")
    return coordinators[0]


def _target_mac(hass: HomeAssistant, call: ServiceCall) -> str:
    """The MAC the call names, directly or through a device tracker."""
    if mac := call.data.get(ATTR_MAC_ADDRESS):
        return mac
    entity_id = call.data[ATTR_ENTITY_ID]
    entry = er.async_get(hass).async_get(entity_id)
    # a tracker's unique_id *is* its MAC (ScannerEntity.unique_id)
    if (
        entry is None
        or entry.platform != DOMAIN
        or entry.domain != "device_tracker"
    ):
        raise HomeAssistantError(
            f"{entity_id} is not a Huawei ONT device tracker"
        )
    return entry.unique_id


def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SET_STATIC_IP):
        return

    async def _set(call: ServiceCall) -> None:
        await _coordinator(hass).async_set_static_ip(
            _target_mac(hass, call), call.data.get(ATTR_IP_ADDRESS)
        )

    async def _clear(call: ServiceCall) -> None:
        await _coordinator(hass).async_clear_static_ip(_target_mac(hass, call))

    hass.services.async_register(
        DOMAIN, SERVICE_SET_STATIC_IP, _set, schema=SET_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_STATIC_IP, _clear, schema=CLEAR_SCHEMA
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    for service in (SERVICE_SET_STATIC_IP, SERVICE_CLEAR_STATIC_IP):
        hass.services.async_remove(DOMAIN, service)
