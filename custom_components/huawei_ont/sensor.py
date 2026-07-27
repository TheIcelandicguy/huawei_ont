"""Sensor platform for Huawei ONT."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import RouterData
from .const import DOMAIN
from .coordinator import HuaweiOntCoordinator


@dataclass(frozen=True, kw_only=True)
class HuaweiOntSensorDescription(SensorEntityDescription):
    value_fn: Callable[[RouterData], int | float | str | None]


def _format_uptime(data: RouterData) -> str | None:
    secs = data.wan_uptime
    if not secs:
        return None
    days = secs // 86400
    hours = (secs % 86400) // 3600
    mins = (secs % 3600) // 60
    return f"{days}d {hours}h {mins}m"


SENSOR_DESCRIPTIONS: tuple[HuaweiOntSensorDescription, ...] = (
    HuaweiOntSensorDescription(
        key="cpu_usage",
        translation_key="cpu_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:cpu-64-bit",
        value_fn=lambda d: d.cpu_usage,
    ),
    HuaweiOntSensorDescription(
        key="memory_usage",
        translation_key="memory_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:memory",
        value_fn=lambda d: d.memory_usage,
    ),
    HuaweiOntSensorDescription(
        key="wan_ip",
        translation_key="wan_ip",
        icon="mdi:ip-network",
        value_fn=lambda d: d.wan_ip or None,
    ),
    HuaweiOntSensorDescription(
        key="wan_uptime",
        translation_key="wan_uptime",
        icon="mdi:timer-outline",
        value_fn=_format_uptime,
    ),
    HuaweiOntSensorDescription(
        key="bytes_sent",
        translation_key="bytes_sent",
        native_unit_of_measurement=UnitOfInformation.BYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:upload-network",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.bytes_sent,
    ),
    HuaweiOntSensorDescription(
        key="bytes_received",
        translation_key="bytes_received",
        native_unit_of_measurement=UnitOfInformation.BYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:download-network",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.bytes_received,
    ),
    HuaweiOntSensorDescription(
        key="packets_sent",
        translation_key="packets_sent",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:package-up",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.packets_sent,
    ),
    HuaweiOntSensorDescription(
        key="packets_received",
        translation_key="packets_received",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:package-down",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.packets_received,
    ),
    HuaweiOntSensorDescription(
        key="connected_devices",
        translation_key="connected_devices",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:devices",
        value_fn=lambda d: d.connected_device_count,
    ),
    HuaweiOntSensorDescription(
        key="wifi_clients",
        translation_key="wifi_clients",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:wifi",
        value_fn=lambda d: d.wifi_client_count,
    ),
    HuaweiOntSensorDescription(
        key="lan_clients",
        translation_key="lan_clients",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:ethernet",
        value_fn=lambda d: d.lan_client_count,
    ),
    HuaweiOntSensorDescription(
        key="download_rate",
        translation_key="download_rate",
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:download",
        value_fn=lambda d: d.download_rate_mbps,
    ),
    HuaweiOntSensorDescription(
        key="upload_rate",
        translation_key="upload_rate",
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:upload",
        value_fn=lambda d: d.upload_rate_mbps,
    ),
    HuaweiOntSensorDescription(
        key="optical_temperature",
        translation_key="optical_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.optical_temperature,
    ),
    HuaweiOntSensorDescription(
        key="optical_voltage",
        translation_key="optical_voltage",
        native_unit_of_measurement="mV",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:flash",
        value_fn=lambda d: d.optical_voltage,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HuaweiOntCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        HuaweiOntSensor(coordinator, description, entry)
        for description in SENSOR_DESCRIPTIONS
    )


class HuaweiOntSensor(CoordinatorEntity[HuaweiOntCoordinator], SensorEntity):
    """Sensor for Huawei ONT data."""

    entity_description: HuaweiOntSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HuaweiOntCoordinator,
        description: HuaweiOntSensorDescription,
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
    def native_value(self) -> int | float | str | None:
        return self.entity_description.value_fn(self.coordinator.data)
