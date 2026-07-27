"""Binary sensor platform for Huawei ONT."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import RouterData
from .const import DOMAIN
from .coordinator import HuaweiOntCoordinator


@dataclass(frozen=True, kw_only=True)
class HuaweiOntBinarySensorDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[RouterData], bool]


BINARY_SENSOR_DESCRIPTIONS: tuple[HuaweiOntBinarySensorDescription, ...] = (
    HuaweiOntBinarySensorDescription(
        key="wan_connection",
        translation_key="wan_connection",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        is_on_fn=lambda d: d.wan_status == "Connected",
    ),
    HuaweiOntBinarySensorDescription(
        key="ont_status",
        translation_key="ont_status",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        is_on_fn=lambda d: d.ont_state == "ONLINE",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HuaweiOntCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        HuaweiOntBinarySensor(coordinator, description, entry)
        for description in BINARY_SENSOR_DESCRIPTIONS
    ]
    entities.extend(
        HuaweiOntLanPortSensor(coordinator, port.port, entry)
        for port in coordinator.data.lan_ports
    )
    async_add_entities(entities)


class HuaweiOntBinarySensor(
    CoordinatorEntity[HuaweiOntCoordinator], BinarySensorEntity
):
    """Binary sensor for Huawei ONT."""

    entity_description: HuaweiOntBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HuaweiOntCoordinator,
        description: HuaweiOntBinarySensorDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Huawei {coordinator.data.model}",
            manufacturer="Huawei",
            model=coordinator.data.model,
            sw_version=coordinator.data.software_version,
            hw_version=coordinator.data.hardware_version,
        )

    @property
    def is_on(self) -> bool:
        return self.entity_description.is_on_fn(self.coordinator.data)


class HuaweiOntLanPortSensor(
    CoordinatorEntity[HuaweiOntCoordinator], BinarySensorEntity
):
    """Link status for a LAN port on the Huawei ONT."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: HuaweiOntCoordinator,
        port: int,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._port = port
        self._attr_unique_id = f"{entry.entry_id}_lan_port_{port}"
        self._attr_name = f"LAN{port} link"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Huawei {coordinator.data.model}",
            manufacturer="Huawei",
            model=coordinator.data.model,
            sw_version=coordinator.data.software_version,
            hw_version=coordinator.data.hardware_version,
        )

    def _port_data(self):
        for p in self.coordinator.data.lan_ports:
            if p.port == self._port:
                return p
        return None

    @property
    def is_on(self) -> bool:
        p = self._port_data()
        return bool(p and p.link_up)

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        p = self._port_data()
        if not p:
            return {}
        return {"speed": p.speed, "duplex": p.duplex}
