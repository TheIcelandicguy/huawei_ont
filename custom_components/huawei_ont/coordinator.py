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

    def holder_of(self, ip: str, mac: str) -> str | None:
        """Name of an online device other than `mac` that holds `ip` now."""
        for device in self.data.devices:
            if (
                device.ip_address == ip
                and device.status == "Online"
                and device.mac_address.lower() != mac.lower()
            ):
                name = device.hostname
                if not name or name == "--":
                    name = device.mac_address.lower()
                return name
        return None

    async def async_set_static_ip(
        self, mac: str, ip: str | None = None, force: bool = False
    ) -> None:
        """Reserve `ip` for `mac`; with no `ip`, pin the address it has now.

        The router only checks the new address against other *reservations*.
        A device that already holds it on a dynamic lease keeps it until that
        lease runs out, so two devices would share the address meanwhile. Say
        so up front unless the caller insists with `force`.
        """
        try:
            mac = normalize_mac(mac)
            if ip is None:
                ip = self.current_ip(mac)
                if ip is None:
                    raise ValueError(
                        f"{mac} is not on the router's device list, so there "
                        "is no current address to pin — pass ip_address"
                    )
            elif not force and (holder := self.holder_of(ip, mac)):
                raise ValueError(
                    f"{ip} is in use right now by {holder}. Reconnect or "
                    "restart that device first so it takes another address, "
                    "or pass force to reserve it anyway"
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
