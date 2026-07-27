"""Device tracker platform for Huawei ONT.

Only-online mode: a tracker exists only while its device is currently
connected. The router returns its full DHCP lease history (hundreds of
devices), so we filter to online devices and prune trackers once a device
has been gone for a short grace period. A one-time registry cleanup at
setup removes stale trackers left over from earlier "track everything"
behaviour.
"""

from __future__ import annotations

import logging

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ConnectedDevice
from .const import DOMAIN
from .coordinator import HuaweiOntCoordinator
from .oui import preload as preload_oui
from .oui import short_label, vendor

_LOGGER = logging.getLogger(__name__)

# how many consecutive polls a device may be absent before its tracker is
# removed — a short grace window absorbs brief Wi-Fi drops without churn
PRUNE_GRACE = 3


def _valid_mac(mac: str) -> bool:
    return bool(mac) and mac != "00:00:00:00:00:00"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HuaweiOntCoordinator = hass.data[DOMAIN][entry.entry_id]
    tracked: dict[str, HuaweiOntDeviceTracker] = {}
    missing: dict[str, int] = {}

    # warm the OUI table off the event loop so the first vendor lookup
    # (in an entity property getter) doesn't block
    await hass.async_add_executor_job(preload_oui)

    @callback
    def _online_devices() -> dict[str, ConnectedDevice]:
        result: dict[str, ConnectedDevice] = {}
        for d in coordinator.data.devices:
            mac = d.mac_address.lower()
            if d.status == "Online" and _valid_mac(mac):
                result[mac] = d
        return result

    # one-time cleanup: drop trackers left in the registry for devices that
    # are not currently online (clears the lease-history backlog). Trackers
    # the user has renamed are kept — a rename pins the device.
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    online = _online_devices()
    removed = 0
    for reg_entry in er.async_entries_for_config_entry(
        registry, entry.entry_id
    ):
        if reg_entry.domain != "device_tracker":
            continue
        if reg_entry.name:  # user-renamed → keep
            continue
        mac = (
            reg_entry.unique_id[len(prefix):]
            if reg_entry.unique_id.startswith(prefix)
            else ""
        )
        if mac not in online:
            registry.async_remove(reg_entry.entity_id)
            removed += 1
    if removed:
        _LOGGER.debug("Pruned %d stale device trackers at setup", removed)

    @callback
    def _async_update_devices() -> None:
        online = _online_devices()

        # add trackers for newly-online devices
        new_entities = []
        for mac, device in online.items():
            missing.pop(mac, None)
            if mac not in tracked:
                entity = HuaweiOntDeviceTracker(coordinator, device, entry)
                tracked[mac] = entity
                new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities)

        # prune trackers whose device has been offline beyond the grace
        # window — but keep user-renamed ones (they show not_home instead)
        reg = er.async_get(hass)
        for mac in list(tracked):
            if mac in online:
                continue
            entity = tracked[mac]
            reg_entry = (
                reg.async_get(entity.entity_id) if entity.entity_id else None
            )
            if reg_entry and reg_entry.name:  # user-renamed → keep, show away
                missing.pop(mac, None)
                continue
            missing[mac] = missing.get(mac, 0) + 1
            if missing[mac] >= PRUNE_GRACE:
                tracked.pop(mac)
                missing.pop(mac, None)
                if reg_entry:
                    reg.async_remove(entity.entity_id)

    _async_update_devices()
    entry.async_on_unload(
        coordinator.async_add_listener(_async_update_devices)
    )


class HuaweiOntDeviceTracker(
    CoordinatorEntity[HuaweiOntCoordinator], ScannerEntity
):
    """Device tracker for a device connected to the Huawei ONT."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HuaweiOntCoordinator,
        device: ConnectedDevice,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._mac = device.mac_address.lower()
        self._attr_unique_id = f"{entry.entry_id}_{self._mac}"
        self._entry_id = entry.entry_id

    @property
    def source_type(self) -> SourceType:
        return SourceType.ROUTER

    @property
    def name(self) -> str:
        hostname = self._device.hostname
        if hostname and hostname != "--":
            return hostname
        # no DHCP hostname: fall back to a vendor-based label
        return short_label(self._mac)

    @property
    def is_connected(self) -> bool:
        return self._device.status == "Online"

    @property
    def ip_address(self) -> str | None:
        return self._device.ip_address or None

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def hostname(self) -> str | None:
        h = self._device.hostname
        return h if h and h != "--" else None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        attrs = {
            "interface": self._device.interface,
            "device_type": self._device.device_type,
            "online_duration": self._device.online_duration,
        }
        v = vendor(self._mac)
        if v:
            attrs["vendor"] = v
        return attrs

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=f"Huawei {self.coordinator.data.model}",
            manufacturer="Huawei",
            model=self.coordinator.data.model,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        for device in self.coordinator.data.devices:
            if device.mac_address.lower() == self._mac:
                self._device = device
                break
        super()._handle_coordinator_update()
