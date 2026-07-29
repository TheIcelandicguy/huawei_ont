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


def _mac_from_unique_id(unique_id: str) -> str:
    """MAC behind a tracker's registry entry.

    ScannerEntity defines unique_id as a property returning mac_address, which
    shadows _attr_unique_id, so a tracker's unique_id is the bare lowercase MAC
    and nothing else. Anything deriving a MAC from the registry has to agree
    with that or it silently matches nothing.
    """
    return unique_id.strip().lower()


def _is_random_mac(mac: str) -> bool:
    """True for a locally-administered (randomised) MAC address."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def _find_rotated_twin(
    hostname: str,
    online_hostnames: list[str],
    candidates: list[tuple[str, str]],
) -> str | None:
    """Return the MAC of the stale tracker this hostname has rotated away from.

    `candidates` is (mac, hostname) for existing trackers whose device is not
    currently online. Every guard fails closed: a stranded duplicate tracker is
    recoverable, but wrongly fusing two devices into one silently corrupts
    whatever presence automations depend on them.
    """
    if not hostname:
        # no DHCP hostname means no stable identity to match on
        return None
    key = hostname.casefold()
    # a second live device under the same name makes the match ambiguous
    if sum(1 for h in online_hostnames if h.casefold() == key) != 1:
        return None
    matches = [
        mac
        for mac, name in candidates
        if name and name.casefold() == key and _is_random_mac(mac)
    ]
    return matches[0] if len(matches) == 1 else None


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

    # startup cleanup: drop trackers left in the registry for devices that are
    # not currently online (clears the lease-history backlog). Trackers the
    # user has renamed are kept — a rename pins the device.
    registry = er.async_get(hass)
    online = _online_devices()
    removed = 0
    for reg_entry in er.async_entries_for_config_entry(
        registry, entry.entry_id
    ):
        if reg_entry.domain != "device_tracker":
            continue
        if reg_entry.name:  # user-renamed → keep
            continue
        mac = _mac_from_unique_id(reg_entry.unique_id)
        if mac not in online:
            registry.async_remove(reg_entry.entity_id)
            removed += 1
    if removed:
        _LOGGER.debug("Pruned %d stale device trackers at setup", removed)

    @callback
    def _migrate_rotated_macs(
        online: dict[str, ConnectedDevice], reg: er.EntityRegistry
    ) -> None:
        """Follow devices that reappeared under a fresh randomised MAC.

        A phone that randomises its MAC comes back looking like a brand-new
        device, which would strand its old tracker — renamed trackers are
        pinned and never pruned — right next to a duplicate. Match on the DHCP
        hostname and re-point the existing tracker instead, so its entity_id,
        its name and any automations referencing it survive the rotation.
        """
        new_macs = [mac for mac in online if mac not in tracked]
        if not new_macs:
            return

        reg_entries = [
            e
            for e in er.async_entries_for_config_entry(reg, entry.entry_id)
            if e.domain == "device_tracker"
        ]
        known_ids = {e.unique_id for e in reg_entries}
        online_hostnames = [d.hostname for d in online.values() if d.hostname]

        for mac in new_macs:
            if mac in known_ids:
                continue  # a device we already know coming back, not a rotation
            candidates = [
                (candidate_mac, e.original_name or "")
                for e in reg_entries
                for candidate_mac in (_mac_from_unique_id(e.unique_id),)
                if candidate_mac and candidate_mac not in online
            ]
            hostname = online[mac].hostname
            old_mac = _find_rotated_twin(hostname, online_hostnames, candidates)
            if not old_mac:
                continue
            old_entry = next(
                (
                    e
                    for e in reg_entries
                    if _mac_from_unique_id(e.unique_id) == old_mac
                ),
                None,
            )
            if old_entry is None:
                continue
            try:
                reg.async_update_entity(old_entry.entity_id, new_unique_id=mac)
            except ValueError as err:
                _LOGGER.debug(
                    "Could not re-key %s from %s to %s: %s",
                    old_entry.entity_id, old_mac, mac, err,
                )
                continue
            _LOGGER.info(
                "%s changed MAC %s -> %s, keeping tracker %s",
                hostname, old_mac, mac, old_entry.entity_id,
            )
            missing.pop(old_mac, None)
            entity = tracked.pop(old_mac, None)
            if entity is not None:
                # the entity object is still alive (it was showing not_home),
                # so re-point it rather than replacing it
                entity.adopt(online[mac])
                tracked[mac] = entity
            # otherwise no entity exists yet (a pin that was offline at
            # startup, showing unavailable); the add pass below creates one
            # that binds to the re-keyed registry entry and inherits its name
            reg_entries = [e for e in reg_entries if e is not old_entry]
            known_ids.discard(old_mac)
            known_ids.add(mac)

    @callback
    def _async_update_devices() -> None:
        online = _online_devices()
        _migrate_rotated_macs(online, er.async_get(hass))

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
        self._entry_id = entry.entry_id
        # no _attr_unique_id: ScannerEntity's unique_id property returns
        # mac_address and would shadow it anyway, so the MAC *is* the key

    @callback
    def adopt(self, device: ConnectedDevice) -> None:
        """Follow the same physical device onto a new MAC address.

        The caller re-keys the registry entry first; updating _mac here keeps
        the entity's own unique_id (via mac_address) in step with it.
        """
        self._device = device
        self._mac = device.mac_address.lower()
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

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
