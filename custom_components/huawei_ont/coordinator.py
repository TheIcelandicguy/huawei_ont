"""Data update coordinator for Huawei ONT."""

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HuaweiOntApi, RouterData
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

    async def _async_update_data(self) -> RouterData:
        try:
            return await self.hass.async_add_executor_job(
                self.api.get_router_data
            )
        except Exception as err:
            raise UpdateFailed(f"Error fetching router data: {err}") from err
