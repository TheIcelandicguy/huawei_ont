"""Huawei OptiXstar ONT integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .api import HuaweiOntApi
from .const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USE_HTTPS,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USE_HTTPS,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import HuaweiOntCoordinator
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    api = HuaweiOntApi(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        use_https=entry.data.get(CONF_USE_HTTPS, DEFAULT_USE_HTTPS),
    )

    scan_interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = HuaweiOntCoordinator(hass, api, scan_interval)

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    # The Static IP switches live on a sub-device of the ONT. Both devices are
    # made here, parent first, because a DeviceInfo on an entity can only name
    # its parent with the deprecated `via_device`.
    devices = dr.async_get(hass)
    hub = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Huawei {coordinator.data.model}",
        manufacturer="Huawei",
        model=coordinator.data.model,
    )
    sub_device = dict(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_static_ip")},
        name="Static IP reservations",
        manufacturer="Huawei",
        model=coordinator.data.model,
    )
    try:
        devices.async_get_or_create(**sub_device, via_device_id=hub.id)
    except TypeError:
        # Home Assistant before via_device_id existed
        devices.async_get_or_create(
            **sub_device, via_device=(DOMAIN, entry.entry_id)
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: HuaweiOntCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.api.close()
        if not hass.data[DOMAIN]:
            async_unregister_services(hass)
    return unload_ok
