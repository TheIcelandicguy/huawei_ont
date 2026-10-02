"""Tests for the DHCP static IP services, switch and tracker attribute.

Like test_device_tracker these need Home Assistant (Linux only). The router is
a FakeApi that records the reservation calls and serves a reservation table.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.huawei_ont.api import HuaweiOntError, StaticBinding
from custom_components.huawei_ont.const import DOMAIN

from test_device_tracker import (  # noqa: F401  (fixture re-export)
    FakeApi,
    LAPTOP_MAC,
    device,
    enable_custom_components,
    make_entry,
    poll,
    router_data,
    setup_entry,
    tracker_entities,
)


class StaticFakeApi(FakeApi):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.bindings: list[StaticBinding] = []
        self.calls: list[tuple] = []
        self.set_error: Exception | None = None

    def get_router_data(self):
        data = super().get_router_data()
        data.static_bindings = list(self.bindings)
        return data

    def set_static_ip(self, mac, ip):
        self.calls.append(("set", mac, ip))
        if self.set_error:
            raise self.set_error

    def remove_static_ip(self, mac):
        self.calls.append(("clear", mac))
        return True


@pytest.fixture
def fake_api():
    api = StaticFakeApi()
    api.data = router_data(device(LAPTOP_MAC, "laptop", ip="192.168.0.42"))
    with patch("custom_components.huawei_ont.HuaweiOntApi", return_value=api):
        yield api


async def test_set_static_ip_pins_the_current_address(hass: HomeAssistant, fake_api):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    tracker = tracker_entities(er.async_get(hass), entry.entry_id)[LAPTOP_MAC]

    await hass.services.async_call(
        DOMAIN, "set_static_ip", {"entity_id": tracker}, blocking=True
    )

    assert fake_api.calls == [("set", LAPTOP_MAC, "192.168.0.42")]


async def test_set_static_ip_takes_an_explicit_address_for_a_bare_mac(
    hass: HomeAssistant, fake_api
):
    await setup_entry(hass, make_entry(hass))

    await hass.services.async_call(
        DOMAIN,
        "set_static_ip",
        {"mac_address": "AA-BB-CC-DD-EE-FF", "ip_address": "192.168.0.60"},
        blocking=True,
    )

    assert fake_api.calls[0][1:] == ("aa:bb:cc:dd:ee:ff", "192.168.0.60")


async def test_pinning_a_device_the_router_does_not_list_needs_an_address(
    hass: HomeAssistant, fake_api
):
    await setup_entry(hass, make_entry(hass))

    with pytest.raises(HomeAssistantError, match="ip_address"):
        await hass.services.async_call(
            DOMAIN,
            "set_static_ip",
            {"mac_address": "aa:bb:cc:dd:ee:ff"},
            blocking=True,
        )
    assert fake_api.calls == []


async def test_a_router_refusal_surfaces_as_a_service_error(
    hass: HomeAssistant, fake_api
):
    await setup_entry(hass, make_entry(hass))
    fake_api.set_error = HuaweiOntError("192.168.0.60 is already reserved")

    with pytest.raises(HomeAssistantError, match="already reserved"):
        await hass.services.async_call(
            DOMAIN,
            "set_static_ip",
            {"mac_address": LAPTOP_MAC, "ip_address": "192.168.0.60"},
            blocking=True,
        )


async def test_clear_static_ip_releases_the_reservation(
    hass: HomeAssistant, fake_api
):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    tracker = tracker_entities(er.async_get(hass), entry.entry_id)[LAPTOP_MAC]

    await hass.services.async_call(
        DOMAIN, "clear_static_ip", {"entity_id": tracker}, blocking=True
    )

    assert fake_api.calls == [("clear", LAPTOP_MAC)]


async def test_a_foreign_entity_is_not_a_target(hass: HomeAssistant, fake_api):
    await setup_entry(hass, make_entry(hass))

    with pytest.raises(HomeAssistantError, match="not a Huawei ONT"):
        await hass.services.async_call(
            DOMAIN, "set_static_ip", {"entity_id": "light.kitchen"},
            blocking=True,
        )


async def test_the_tracker_shows_its_reservation(hass: HomeAssistant, fake_api):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    tracker = tracker_entities(er.async_get(hass), entry.entry_id)[LAPTOP_MAC]
    assert "static_ip" not in hass.states.get(tracker).attributes

    fake_api.bindings = [
        StaticBinding("x.1", True, "192.168.0.42", LAPTOP_MAC)
    ]
    await poll(hass, entry)

    assert hass.states.get(tracker).attributes["static_ip"] == "192.168.0.42"


async def test_each_device_gets_a_switch_that_starts_disabled(
    hass: HomeAssistant, fake_api
):
    entry = make_entry(hass)
    await setup_entry(hass, entry)

    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id(
        "switch", DOMAIN, f"{entry.entry_id}_static_ip_{LAPTOP_MAC}"
    )
    assert entity_id is not None
    assert reg.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    # grouped under Configuration, not mixed in with the Wi-Fi switches
    assert reg.async_get(entity_id).entity_category is EntityCategory.CONFIG


async def test_the_services_go_away_with_the_last_entry(
    hass: HomeAssistant, fake_api
):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    assert hass.services.has_service(DOMAIN, "set_static_ip")

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, "set_static_ip")
