"""Data update coordinator for Huawei ONT."""

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HuaweiOntApi, HuaweiOntError, RouterData, normalize_mac
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class HuaweiOntCoordinator(DataUpdateCoordinator[RouterData]):
    """Coordinator to manage fetching data from the router."""

    def __init__(
        self, hass: HomeAssistant, api: HuaweiOntApi, scan_interval: int
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.api = api

    def current_ip(self, mac: str) -> str | None:
        """The IPv4 address the router last showed for `mac`, if any."""
        for device in self.data.devices:
            if device.mac_address.lower() == mac.lower():
                return device.ip_address or None
        return None

    def reserved_ip(self, mac: str) -> str | None:
        """The address reserved for `mac`, None if none or not known yet."""
        for binding in self.data.static_bindings or ():
            if binding.mac == mac.lower() and binding.enabled:
                return binding.ip
        return None

    async def async_set_static_ip(self, mac: str, ip: str | None = None) -> None:
        """Reserve `ip` for `mac`; with no `ip`, pin the address it has now."""
        try:
            mac = normalize_mac(mac)
            if ip is None:
                ip = self.current_ip(mac)
                if ip is None:
                    raise ValueError(
                        f"{mac} is not on the router's device list, so there "
                        "is no current address to pin — pass ip_address"
                    )
            await self.hass.async_add_executor_job(
                self.api.set_static_ip, mac, ip
            )
        except (ValueError, HuaweiOntError) as err:
            raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()

    async def async_clear_static_ip(self, mac: str) -> None:
        """Drop the reservation for `mac` (a no-op if it has none)."""
        try:
            mac = normalize_mac(mac)
            await self.hass.async_add_executor_job(
                self.api.remove_static_ip, mac
            )
        except (ValueError, HuaweiOntError) as err:
            raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()

    async def _async_update_data(self) -> RouterData:
        try:
            return await self.hass.async_add_executor_job(
                self.api.get_router_data
            )
        except Exception as err:
            raise UpdateFailed(f"Error fetching router data: {err}") from err
