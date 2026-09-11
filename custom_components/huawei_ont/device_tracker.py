"""Device tracker platform for Huawei ONT.

Only-online mode: a tracker exists only while its device is currently
connected. The router returns its full DHCP lease history (hundreds of
devices), so we filter to online devices and prune trackers once a device
has been gone for PRUNE_AFTER. A registry cleanup at setup removes
stale trackers left over from earlier "track everything" behaviour.

Every removal here is driven by a device's absence from the router's list, so
nothing in this module may act on a list the router did not actually return —
see `device_list_valid`.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import ConnectedDevice
from .const import DOMAIN
from .coordinator import HuaweiOntCoordinator
from .oui import preload as preload_oui
from .oui import short_label, vendor

_LOGGER = logging.getLogger(__name__)

# How long a device may be absent before its tracker is removed. Measured on
# the development install over 30 days: with the old three-poll (90 s) grace,
# 75 trackers were removed and re-created 21,956 times — Wi-Fi clients and
# Shelly wall displays dropping off the list for a minute or two. 97.8% were
# back within 5 minutes and 98.9% within 10; past that the curve is flat
# (99.1% at an hour), so the rest had genuinely left. A time rather than a
# poll count, so it does not shrink with a shorter scan interval.
PRUNE_AFTER = timedelta(minutes=10)


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
    new_mac: str,
    hostname: str,
    online_hostnames: list[str],
    known_hostnames: dict[str, str],
    candidates: list[tuple[str, str]],
) -> str | None:
    """Return the MAC of the stale tracker this hostname has rotated away from.

    `online_hostnames` is every hostname currently online, `known_hostnames`
    maps MAC to hostname across everything the router remembers (its lease
    history included), and `candidates` is (mac, hostname) for existing
    trackers whose device is not currently online.

    Every guard fails closed: a stranded duplicate tracker is recoverable, but
    wrongly fusing two devices into one silently corrupts whatever presence
    automations depend on them.
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
    if len(matches) != 1:
        return None
    old_mac = matches[0]
    # Generic DHCP names ("iPhone", "localhost") are not identities. The two
    # MACs of a real rotation legitimately share one, but a third device
    # anywhere in the router's lease history answering to the same name means
    # the name singles out nobody — refuse rather than fuse two devices.
    if any(
        mac not in (new_mac, old_mac) and name and name.casefold() == key
        for mac, name in known_hostnames.items()
    ):
        return None
    return old_mac


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HuaweiOntCoordinator = hass.data[DOMAIN][entry.entry_id]
    tracked: dict[str, HuaweiOntDeviceTracker] = {}
    # when each tracked device was first missing from the router's list
    missing: dict[str, datetime] = {}

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

    # Startup cleanup: drop trackers left in the registry for devices that are
    # not currently online (clears the lease-history backlog). Trackers the
    # user has renamed are kept — a rename pins the device.
    #
    # This runs on every restart and reload, so it must never fire on a list
    # the router did not return: an unreachable router at Home Assistant start
    # would otherwise wipe every unpinned tracker in one go.
    if coordinator.data.device_list_valid:
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
        # every MAC the router remembers, lease history included — a name that
        # is not unique across all of it cannot identify a rotated device
        known_hostnames = {
            d.mac_address.lower(): d.hostname
            for d in coordinator.data.devices
        }

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
            old_mac = _find_rotated_twin(
                mac, hostname, online_hostnames, known_hostnames, candidates
            )
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
        if not coordinator.data.device_list_valid:
            # The router answered, but not with a device list. Reading that as
            # "everybody left" would age every tracker out of the grace window
            # and delete the lot once PRUNE_AFTER ran out.
            return
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

        # prune trackers whose device has been gone for PRUNE_AFTER — but
        # keep user-renamed ones (they show not_home instead)
        now = dt_util.utcnow()
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
            if now - missing.setdefault(mac, now) >= PRUNE_AFTER:
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
        # No online_duration: it moves on every poll, which made every poll a
        # new state and a new recorder row per tracker — 2.2M rows a month
        # from 72 trackers on the development install.
        attrs = {
            "interface": self._device.interface,
            "device_type": self._device.device_type,
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
        if not self.coordinator.data.device_list_valid:
            # no list this poll: keep reporting what we last knew
            super()._handle_coordinator_update()
            return
        for device in self.coordinator.data.devices:
            if device.mac_address.lower() == self._mac:
                self._device = device
                break
        else:
            # The router dropped this MAC from its list entirely. Pinned
            # trackers are never pruned, so without this they would sit on
            # their last-seen data and report home indefinitely.
            if self._device.status == "Online":
                self._device = replace(self._device, status="Offline")
        super()._handle_coordinator_update()
