"""Tests for the device_tracker platform.

These need Home Assistant, so they run on Linux only (see conftest). The router
is replaced by a fake API object whose RouterData the test sets directly, which
is where the interesting cases live: a poll that returned no device list at all
must not be mistaken for a router with nobody connected.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_HOME, STATE_NOT_HOME, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.huawei_ont.api import (
    ConnectedDevice,
    HuaweiOntConnectionError,
    RouterData,
)
from custom_components.huawei_ont.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)
from custom_components.huawei_ont.device_tracker import (
    PRUNE_GRACE,
    _find_rotated_twin,
    _is_random_mac,
    _mac_from_unique_id,
)

# locally-administered (randomised) MACs, as a phone rotating its MAC produces
PHONE_MAC = "a2:11:22:33:44:01"
PHONE_MAC_ROTATED = "a2:11:22:33:44:02"
# burned-in, so the locally-administered bit is clear (0xa8, not 0xaa)
LAPTOP_MAC = "a8:bb:cc:dd:ee:10"


@pytest.fixture(autouse=True)
def enable_custom_components(enable_custom_integrations):
    """Let Home Assistant load the integration out of custom_components/."""
    yield


def device(
    mac: str,
    hostname: str = "",
    status: str = "Online",
    ip: str = "192.168.0.5",
    interface: str = "SSID1",
) -> ConnectedDevice:
    return ConnectedDevice(
        hostname=hostname,
        ip_address=ip,
        mac_address=mac,
        status=status,
        interface=interface,
        device_type="Phone",
        online_duration="600",
    )


def router_data(*devices: ConnectedDevice, list_valid: bool = True) -> RouterData:
    data = RouterData(wan_status="Connected", ont_state="ONLINE")
    if list_valid:
        data.device_list_valid = True
        data.devices = list(devices)
        data.connected_device_count = sum(
            1 for d in devices if d.status == "Online"
        )
    return data


class FakeApi:
    """Stands in for HuaweiOntApi; the test drives `data` and `error`."""

    def __init__(self, *args, **kwargs) -> None:
        self.data = router_data()
        self.error: Exception | None = None
        self.polls = 0

    def get_router_data(self) -> RouterData:
        self.polls += 1
        if self.error is not None:
            raise self.error
        return self.data

    def close(self) -> None:
        pass


@pytest.fixture
def fake_api():
    api = FakeApi()
    with patch(
        "custom_components.huawei_ont.HuaweiOntApi", return_value=api
    ):
        yield api


def make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Huawei ONT",
        data={
            CONF_HOST: "192.168.0.1",
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "secret",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return result


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Run one coordinator update with whatever the fake API now returns."""
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()


def tracker_entities(registry: er.EntityRegistry, entry_id: str) -> dict[str, str]:
    """unique_id -> entity_id for every tracker registered to the entry."""
    return {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry_id)
        if e.domain == "device_tracker"
    }


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


def test_is_random_mac_reads_the_locally_administered_bit():
    assert _is_random_mac("a2:11:22:33:44:55")  # 0xa2 & 0x02
    assert _is_random_mac("aa:bb:cc:dd:ee:ff")  # 0xaa & 0x02 — also random
    assert not _is_random_mac("a8:bb:cc:dd:ee:ff")
    assert not _is_random_mac("00:11:22:33:44:55")
    assert not _is_random_mac("nonsense")


def test_mac_from_unique_id_matches_scannerentitys_bare_mac():
    # ScannerEntity.unique_id is the mac_address property and nothing else
    assert _mac_from_unique_id("  AA:BB:CC:DD:EE:FF ") == "aa:bb:cc:dd:ee:ff"


def test_rotated_twin_matches_a_single_offline_namesake():
    assert _find_rotated_twin(
        PHONE_MAC_ROTATED,
        "Davids-iPhone",
        ["Davids-iPhone"],
        {PHONE_MAC: "Davids-iPhone", PHONE_MAC_ROTATED: "Davids-iPhone"},
        [(PHONE_MAC, "Davids-iPhone")],
    ) == PHONE_MAC


def test_rotated_twin_refuses_a_name_a_third_device_also_answers_to():
    # "iPhone" is a factory default, not an identity
    assert _find_rotated_twin(
        PHONE_MAC_ROTATED,
        "iPhone",
        ["iPhone"],
        {
            PHONE_MAC: "iPhone",
            PHONE_MAC_ROTATED: "iPhone",
            "aa:bb:cc:00:00:99": "iPhone",  # someone else's, from lease history
        },
        [(PHONE_MAC, "iPhone")],
    ) is None


def test_rotated_twin_refuses_two_live_devices_under_one_name():
    assert _find_rotated_twin(
        PHONE_MAC_ROTATED,
        "iPhone",
        ["iPhone", "iPhone"],
        {PHONE_MAC: "iPhone", PHONE_MAC_ROTATED: "iPhone"},
        [(PHONE_MAC, "iPhone")],
    ) is None


def test_rotated_twin_refuses_without_a_hostname():
    assert _find_rotated_twin(
        PHONE_MAC_ROTATED, "", [], {}, [(PHONE_MAC, "")]
    ) is None


def test_rotated_twin_refuses_a_stable_mac():
    # a burned-in MAC does not rotate, so a namesake is a different device
    assert _find_rotated_twin(
        PHONE_MAC_ROTATED,
        "Davids-iPhone",
        ["Davids-iPhone"],
        {LAPTOP_MAC: "Davids-iPhone", PHONE_MAC_ROTATED: "Davids-iPhone"},
        [(LAPTOP_MAC, "Davids-iPhone")],
    ) is None


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


async def test_online_devices_get_trackers(
    hass: HomeAssistant, fake_api, entity_registry: er.EntityRegistry
):
    fake_api.data = router_data(
        device(PHONE_MAC, "Davids-iPhone"),
        device(LAPTOP_MAC, "Laptop", interface="LAN1"),
    )
    entry = make_entry(hass)
    assert await setup_entry(hass, entry)

    trackers = tracker_entities(entity_registry, entry.entry_id)
    assert set(trackers) == {PHONE_MAC, LAPTOP_MAC}
    assert hass.states.get(trackers[PHONE_MAC]).state == STATE_HOME


async def test_a_device_that_leaves_is_pruned_after_the_grace_window(
    hass: HomeAssistant, fake_api, entity_registry: er.EntityRegistry
):
    fake_api.data = router_data(device(PHONE_MAC, "Davids-iPhone"))
    entry = make_entry(hass)
    assert await setup_entry(hass, entry)

    fake_api.data = router_data()  # a real, empty list: everyone left
    for _ in range(PRUNE_GRACE - 1):
        await poll(hass, entry)
        assert PHONE_MAC in tracker_entities(entity_registry, entry.entry_id)

    await poll(hass, entry)
    assert PHONE_MAC not in tracker_entities(entity_registry, entry.entry_id)


# --------------------------------------------------------------------------
# a poll that brought back no device list
# --------------------------------------------------------------------------


async def test_a_missing_device_list_never_prunes_trackers(
    hass: HomeAssistant, fake_api, entity_registry: er.EntityRegistry
):
    fake_api.data = router_data(device(PHONE_MAC, "Davids-iPhone"))
    entry = make_entry(hass)
    assert await setup_entry(hass, entry)
    entity_id = tracker_entities(entity_registry, entry.entry_id)[PHONE_MAC]

    # the rest of the poll landed, but the device list did not
    fake_api.data = router_data(list_valid=False)
    for _ in range(PRUNE_GRACE + 2):
        await poll(hass, entry)

    assert PHONE_MAC in tracker_entities(entity_registry, entry.entry_id)
    # and it keeps reporting what was last known, rather than going away
    assert hass.states.get(entity_id).state == STATE_HOME


async def test_setup_does_not_prune_when_the_router_returned_no_list(
    hass: HomeAssistant, fake_api, entity_registry: er.EntityRegistry
):
    # a tracker from a previous run of Home Assistant
    entry = make_entry(hass)
    entity_registry.async_get_or_create(
        "device_tracker", DOMAIN, PHONE_MAC,
        config_entry=entry, original_name="Davids-iPhone",
    )

    fake_api.data = router_data(list_valid=False)
    assert await setup_entry(hass, entry)

    assert PHONE_MAC in tracker_entities(entity_registry, entry.entry_id)


async def test_setup_prunes_stale_trackers_when_the_list_is_good(
    hass: HomeAssistant, fake_api, entity_registry: er.EntityRegistry
):
    entry = make_entry(hass)
    entity_registry.async_get_or_create(
        "device_tracker", DOMAIN, "aa:bb:cc:00:00:99",
        config_entry=entry, original_name="LongGone",
    )

    fake_api.data = router_data(device(PHONE_MAC, "Davids-iPhone"))
    assert await setup_entry(hass, entry)

    trackers = tracker_entities(entity_registry, entry.entry_id)
    assert "aa:bb:cc:00:00:99" not in trackers
    assert PHONE_MAC in trackers


async def test_an_unreachable_router_fails_setup_instead_of_emptying_it(
    hass: HomeAssistant, fake_api, entity_registry: er.EntityRegistry
):
    entry = make_entry(hass)
    entity_registry.async_get_or_create(
        "device_tracker", DOMAIN, PHONE_MAC,
        config_entry=entry, original_name="Davids-iPhone",
    )

    fake_api.error = HuaweiOntConnectionError("no route to host")
    assert not await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    # nothing was pruned on the way down
    assert PHONE_MAC in tracker_entities(entity_registry, entry.entry_id)


async def test_a_failed_poll_makes_trackers_unavailable_without_removing_them(
    hass: HomeAssistant, fake_api, entity_registry: er.EntityRegistry
):
    fake_api.data = router_data(device(PHONE_MAC, "Davids-iPhone"))
    entry = make_entry(hass)
    assert await setup_entry(hass, entry)
    entity_id = tracker_entities(entity_registry, entry.entry_id)[PHONE_MAC]

    fake_api.error = HuaweiOntConnectionError("connection reset")
    for _ in range(PRUNE_GRACE + 2):
        await poll(hass, entry)

    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert PHONE_MAC in tracker_entities(entity_registry, entry.entry_id)


# --------------------------------------------------------------------------
# renamed (pinned) trackers
# --------------------------------------------------------------------------


async def test_a_renamed_tracker_survives_but_reports_away(
    hass: HomeAssistant, fake_api, entity_registry: er.EntityRegistry
):
    fake_api.data = router_data(device(PHONE_MAC, "Davids-iPhone"))
    entry = make_entry(hass)
    assert await setup_entry(hass, entry)
    entity_id = tracker_entities(entity_registry, entry.entry_id)[PHONE_MAC]
    entity_registry.async_update_entity(entity_id, name="Davids phone")
    await hass.async_block_till_done()

    # the MAC drops out of the router's list entirely
    fake_api.data = router_data(device(LAPTOP_MAC, "Laptop"))
    for _ in range(PRUNE_GRACE + 2):
        await poll(hass, entry)

    assert PHONE_MAC in tracker_entities(entity_registry, entry.entry_id)
    assert hass.states.get(entity_id).state == STATE_NOT_HOME


async def test_a_pinned_tracker_follows_an_offline_row(
    hass: HomeAssistant, fake_api, entity_registry: er.EntityRegistry
):
    fake_api.data = router_data(device(PHONE_MAC, "Davids-iPhone"))
    entry = make_entry(hass)
    assert await setup_entry(hass, entry)
    entity_id = tracker_entities(entity_registry, entry.entry_id)[PHONE_MAC]
    entity_registry.async_update_entity(entity_id, name="Davids phone")
    await hass.async_block_till_done()

    # still listed, but offline now
    fake_api.data = router_data(device(PHONE_MAC, "Davids-iPhone", status="Offline"))
    await poll(hass, entry)
    assert hass.states.get(entity_id).state == STATE_NOT_HOME

    fake_api.data = router_data(device(PHONE_MAC, "Davids-iPhone"))
    await poll(hass, entry)
    assert hass.states.get(entity_id).state == STATE_HOME


# --------------------------------------------------------------------------
# randomised MAC rotation
# --------------------------------------------------------------------------


async def test_a_rotated_mac_keeps_the_existing_tracker(
    hass: HomeAssistant, fake_api, entity_registry: er.EntityRegistry
):
    fake_api.data = router_data(device(PHONE_MAC, "Davids-iPhone"))
    entry = make_entry(hass)
    assert await setup_entry(hass, entry)
    entity_id = tracker_entities(entity_registry, entry.entry_id)[PHONE_MAC]
    entity_registry.async_update_entity(entity_id, name="Davids phone")
    await hass.async_block_till_done()

    # the phone comes back under a fresh randomised MAC; the old one lingers
    # in the router's lease history
    fake_api.data = router_data(
        device(PHONE_MAC, "Davids-iPhone", status="Offline"),
        device(PHONE_MAC_ROTATED, "Davids-iPhone"),
    )
    await poll(hass, entry)

    trackers = tracker_entities(entity_registry, entry.entry_id)
    assert PHONE_MAC not in trackers
    # same entity, re-keyed — automations referring to it still work
    assert trackers[PHONE_MAC_ROTATED] == entity_id
    assert hass.states.get(entity_id).state == STATE_HOME


async def test_two_devices_sharing_a_generic_hostname_are_not_fused(
    hass: HomeAssistant, fake_api, entity_registry: er.EntityRegistry
):
    fake_api.data = router_data(device(PHONE_MAC, "iPhone"))
    entry = make_entry(hass)
    assert await setup_entry(hass, entry)
    entity_id = tracker_entities(entity_registry, entry.entry_id)[PHONE_MAC]
    entity_registry.async_update_entity(entity_id, name="My phone")
    await hass.async_block_till_done()

    # a *different* phone, also called "iPhone", turns up while the first is
    # offline. Fusing the two would silently corrupt both presence histories.
    fake_api.data = router_data(
        device(PHONE_MAC, "iPhone", status="Offline"),
        device("aa:bb:cc:00:00:99", "iPhone", status="Offline"),
        device(PHONE_MAC_ROTATED, "iPhone"),
    )
    await poll(hass, entry)

    trackers = tracker_entities(entity_registry, entry.entry_id)
    assert trackers[PHONE_MAC] == entity_id  # untouched
    assert PHONE_MAC_ROTATED in trackers
    assert trackers[PHONE_MAC_ROTATED] != entity_id
