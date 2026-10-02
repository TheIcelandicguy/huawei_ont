"""Switch platform for Huawei ONT WiFi networks."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ConnectedDevice, WifiNetwork
from .const import DOMAIN
from .coordinator import HuaweiOntCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HuaweiOntCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[int] = set()

    @callback
    def _async_add_networks() -> None:
        # guest networks are created on the router on first use, so new
        # instances can appear after setup
        new_entities = []
        for network in coordinator.data.wifi_networks:
            if network.instance not in known:
                known.add(network.instance)
                new_entities.append(
                    HuaweiOntWifiSwitch(coordinator, network, entry)
                )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_networks()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_networks))

    known_macs: set[str] = set()

    @callback
    def _async_add_static_ip_switches() -> None:
        if not coordinator.data.device_list_valid:
            return
        new_entities = []
        for device in coordinator.data.devices:
            mac = device.mac_address.lower()
            if mac not in known_macs:
                known_macs.add(mac)
                new_entities.append(
                    HuaweiOntStaticIpSwitch(coordinator, device, entry)
                )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_static_ip_switches()
    entry.async_on_unload(
        coordinator.async_add_listener(_async_add_static_ip_switches)
    )


class HuaweiOntWifiSwitch(
    CoordinatorEntity[HuaweiOntCoordinator], SwitchEntity
):
    """Switch to enable/disable a WiFi network (main or guest)."""

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: HuaweiOntCoordinator,
        network: WifiNetwork,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._instance = network.instance
        self._attr_unique_id = f"{entry.entry_id}_wifi_{network.instance}"
        prefix = "Guest Wi-Fi" if network.is_guest else "Wi-Fi"
        self._attr_name = f"{prefix} {network.band} ({network.ssid})"
        self._attr_icon = (
            "mdi:wifi-lock" if network.is_guest else "mdi:wifi"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Huawei {coordinator.data.model}",
            manufacturer="Huawei",
            model=coordinator.data.model,
            sw_version=coordinator.data.software_version,
            hw_version=coordinator.data.hardware_version,
        )

    def _network(self) -> WifiNetwork | None:
        for n in self.coordinator.data.wifi_networks:
            if n.instance == self._instance:
                return n
        return None

    @property
    def available(self) -> bool:
        return super().available and self._network() is not None

    @property
    def is_on(self) -> bool:
        n = self._network()
        return bool(n and n.enabled)

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        n = self._network()
        if not n:
            return {}
        return {
            "ssid": n.ssid,
            "band": n.band,
            "channel": n.channel,
            "standard": n.standard,
            "guest": str(n.is_guest),
        }

    async def _async_set(self, enabled: bool) -> None:
        ok = await self.hass.async_add_executor_job(
            self.coordinator.api.set_wifi_enabled, self._instance, enabled
        )
        if not ok:
            raise HomeAssistantError(
                f"Failed to set WiFi instance {self._instance} "
                f"to {'on' if enabled else 'off'}"
            )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


class HuaweiOntStaticIpSwitch(
    CoordinatorEntity[HuaweiOntCoordinator], SwitchEntity
):
    """On while the router holds a DHCP reservation for this device.

    Turning it on pins the address the device has right now; turning it off
    releases the reservation. Disabled by default, because one exists per
    device the router has seen and almost nobody wants them all.
    """

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:ip-network"

    def __init__(
        self,
        coordinator: HuaweiOntCoordinator,
        device: ConnectedDevice,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._mac = device.mac_address.lower()
        self._attr_unique_id = f"{entry.entry_id}_static_ip_{self._mac}"
        hostname = device.hostname
        # the plain MAC, not oui.short_label: that reads a CSV from disk, which
        # must not happen on the event loop
        label = hostname if hostname and hostname != "--" else self._mac
        self._attr_name = f"{label} static IP"
        # a sub-device of the ONT, so the reservations get their own page
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_static_ip")},
            name="Static IP reservations",
            manufacturer="Huawei",
            model=coordinator.data.model,
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def available(self) -> bool:
        # unknown reservations must not read as "off"
        return (
            super().available
            and self.coordinator.data.static_bindings is not None
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.reserved_ip(self._mac) is not None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        ip = self.coordinator.reserved_ip(self._mac)
        return {"reserved_ip": ip} if ip else {}

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_static_ip(self._mac)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_clear_static_ip(self._mac)
