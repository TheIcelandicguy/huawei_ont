"""Button platform for Huawei ONT."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HuaweiOntCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HuaweiOntCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HuaweiOntRebootButton(coordinator, entry)])


class HuaweiOntRebootButton(
    CoordinatorEntity[HuaweiOntCoordinator], ButtonEntity
):
    """Button to reboot the Huawei ONT."""

    _attr_has_entity_name = True
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_translation_key = "reboot"

    def __init__(
        self, coordinator: HuaweiOntCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_reboot"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Huawei {coordinator.data.model}",
            manufacturer="Huawei",
            model=coordinator.data.model,
            sw_version=coordinator.data.software_version,
            hw_version=coordinator.data.hardware_version,
        )

    async def async_press(self) -> None:
        ok = await self.hass.async_add_executor_job(self.coordinator.api.reboot)
        if not ok:
            raise HomeAssistantError("Router reboot request failed")
