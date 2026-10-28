"""The services that name devices and draw their cards.

Neither had a test, which is how a device lookup that always returned
nothing survived: `apply_mapping` reported no matches and `generate_cards`
skipped every unit as "has no device", and the suite was green because the
test stub implemented the method the integration was calling. Home
Assistant's `DeviceRegistry` has no such method.

So these tests are deliberately about the plumbing — that the services find
the devices at all — rather than about the planning, which `test_mapping`
already covers.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.daikin_dbacs import async_setup_entry
from custom_components.daikin_dbacs import gateway as gw
from custom_components.daikin_dbacs.const import (
    CONF_DEVICE_INSTANCE,
    CONF_HOST,
    DOMAIN,
    OPT_MEMBERS,
)

import fake_gateway as fake


def run(coro):
    return asyncio.run(coro)


MEMBERS = ["room_temp", "on_off_status"]


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A site directory with a mapping file for instance 12."""
    directory = tmp_path / "site"
    directory.mkdir()
    payload = {
        "1 · 1-00": {"ac_number": 0, "name": "Room A1 ACU A-01",
                          "area": "Block A", "system": "S-01",
                          "outdoor": "CU-S-01", "outdoor_area": "Plant Deck"},
        "1 · 1-01": {"ac_number": 1, "name": "Room A2 ACU A-02",
                          "area": "Block A", "system": "S-01",
                          "outdoor": "CU-S-01"},
    }
    (directory / "gateway-12-units.json").write_text(json.dumps(payload))
    monkeypatch.setattr(
        "custom_components.daikin_dbacs.mapping.SITE_DIR", str(directory)
    )
    return directory


@pytest.fixture
def loaded(monkeypatch, site):
    """One gateway, set up, with a device per live group in the registry."""
    # Like both gateways on the site this was built against: the firmware
    # predates the ventilation members, so they are not published and an
    # entity for one can never read.
    state = fake.FakeGatewayState(
        populated={0, 1}, device_instance=12,
        omit_members=frozenset({32, 33, 34, 35}),
    )

    async def connect(self):
        fake.attach(self, state)
        return await self.read_info()

    monkeypatch.setattr(gw.DbacsGateway, "connect", connect)

    hass = HomeAssistant()
    entry = ConfigEntry(
        data={CONF_HOST: "192.0.2.10", CONF_DEVICE_INSTANCE: 12},
        options={OPT_MEMBERS: MEMBERS},
        entry_id="entry_twelve",
    )
    # Home Assistant's own ConfigEntries holds the entry; the services
    # iterate it to find the gateways, so the stand-in needs it too.
    hass.config_entries.entries.append(entry)
    run(async_setup_entry(hass, entry))

    # The entity platforms are what create group devices in a running
    # Home Assistant, and they do not run here, so stand them in.
    registry = dr.async_get(hass)
    for ac_number in (0, 1):
        registry.add_device(
            f"device_{ac_number}",
            {(DOMAIN, f"{entry.entry_id}_{ac_number}")},
            config_entry_id=entry.entry_id,
        )
    return hass, entry, registry


def call(hass, service, data=None):
    handler = hass.services.registered[(DOMAIN, service)]
    return run(handler(_Call(data or {})))


class _Call:
    def __init__(self, data) -> None:
        self.data = data


class TestTheServicesAreRegistered:
    def test_both_are_there(self, loaded):
        hass, _entry, _registry = loaded
        assert hass.services.has_service(DOMAIN, "apply_mapping")
        assert hass.services.has_service(DOMAIN, "generate_cards")


class TestApplyMappingFindsTheDevices:
    def test_a_dry_run_matches_every_live_group(self, loaded):
        hass, _entry, _registry = loaded
        report = call(hass, "apply_mapping", {"dry_run": True})

        gateway = report["gateways"][0]
        assert gateway["in_the_file_but_not_on_the_gateway"] == []
        assert gateway["change_count"] > 0

    def test_it_reads_the_file_named_after_the_instance(self, loaded):
        hass, _entry, _registry = loaded
        report = call(hass, "apply_mapping", {"dry_run": True})
        assert report["gateways"][0]["mapping_file"].endswith(
            "gateway-12-units.json"
        )

    def test_a_dry_run_changes_nothing(self, loaded):
        hass, _entry, registry = loaded
        # Setup itself links the outdoor units; the dry run adds nothing.
        before = list(registry.updated)
        call(hass, "apply_mapping", {"dry_run": True})
        assert registry.updated == before

    def test_applying_names_and_places_them(self, loaded):
        hass, _entry, registry = loaded
        report = call(hass, "apply_mapping", {"dry_run": False})

        assert report["gateways"][0]["applied_count"] > 0
        assert registry.updated


class TestGenerateCardsFindsTheDevices:
    def test_a_card_for_each_named_group(self, loaded):
        hass, _entry, registry = loaded
        call(hass, "apply_mapping", {"dry_run": False})

        report = call(hass, "generate_cards")
        assert report["skipped"] == []
        assert len(report["cards"]) == 2

    def test_no_cards_claim_a_device_that_is_not_there(self, loaded):
        hass, _entry, _registry = loaded
        report = call(hass, "generate_cards")
        # Unnamed devices are skipped for want of a name, not for want of a
        # device — "has no device" here would mean the lookup is broken.
        assert not any("has no device" in s for s in report["skipped"])


class TestWhichDevicesCount:
    def test_the_gateway_and_outdoor_devices_are_not_groups(self, loaded):
        from custom_components.daikin_dbacs import _group_devices

        hass, entry, registry = loaded
        registry.add_device(
            "outdoor_one", {(DOMAIN, f"{entry.entry_id}_system_S-01")},
            config_entry_id=entry.entry_id,
        )
        registry.add_device(
            "hub_one", {(DOMAIN, entry.entry_id)},
            config_entry_id=entry.entry_id,
        )

        found = _group_devices(registry, entry)
        assert sorted(found) == [0, 1]

    def test_another_entrys_devices_are_not_claimed(self, loaded):
        from custom_components.daikin_dbacs import _group_devices

        hass, entry, registry = loaded
        registry.add_device(
            "other_group", {(DOMAIN, "entry_eleven_0")},
            config_entry_id="entry_eleven",
        )

        found = _group_devices(registry, entry)
        assert sorted(found) == [0, 1]
        assert found[0].id != "other_group"


class TestPurgeOrphans:
    """Removing entities for points the gateway does not publish.

    These accumulate when a gateway is first set up without its member map
    — discovery falls back to probing, which finds the units but not which
    members each one publishes, so an entity is created for every member
    this integration models. On a gateway whose firmware predates the
    ventilation members, that left 252 entities that can never read.

    Once the member map is known they are not created at all. This service
    is for the ones already in the registry.
    """

    def _orphan(self, hass, entry, ac_number=0, key="ventilation_mode_status"):
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        registry.add(
            f"sensor.dbacs_{ac_number}_{key}",
            f"{entry.entry_id}_{ac_number}_{key}",
        )
        return registry

    def test_a_dry_run_lists_them_and_removes_nothing(self, loaded):
        hass, entry, _devices = loaded
        registry = self._orphan(hass, entry)

        report = call(hass, "purge_orphans", {"dry_run": True})

        assert report["orphans"] == ["sensor.dbacs_0_ventilation_mode_status"]
        assert report["removed"] == 0
        assert registry.removed == []

    def test_applying_removes_them(self, loaded):
        hass, entry, _devices = loaded
        registry = self._orphan(hass, entry)

        report = call(hass, "purge_orphans", {"dry_run": False})

        assert report["removed"] == 1
        assert registry.removed == ["sensor.dbacs_0_ventilation_mode_status"]

    def test_a_published_point_is_never_removed(self, loaded):
        """Even when the user has switched it off in the options."""
        hass, entry, _devices = loaded
        registry = self._orphan(hass, entry, key="room_temp")

        report = call(hass, "purge_orphans", {"dry_run": False})

        assert report["orphans"] == []
        assert registry.removed == []

    def test_a_point_on_a_group_that_is_not_live_is_an_orphan(self, loaded):
        hass, entry, _devices = loaded
        registry = self._orphan(hass, entry, ac_number=200, key="room_temp")

        report = call(hass, "purge_orphans", {"dry_run": False})

        assert report["orphans"] == ["sensor.dbacs_200_room_temp"]
        assert registry.removed == ["sensor.dbacs_200_room_temp"]

    def test_the_climate_entity_is_left_alone(self, loaded):
        """Its unique id ends in `climate`, which is not a member key."""
        hass, entry, _devices = loaded
        registry = self._orphan(hass, entry, key="climate")

        report = call(hass, "purge_orphans", {"dry_run": False})

        assert report["orphans"] == []
        assert registry.removed == []

    def test_the_fault_event_is_left_alone(self, loaded):
        hass, entry, _devices = loaded
        registry = self._orphan(hass, entry, key="fault_event")

        report = call(hass, "purge_orphans", {"dry_run": False})
        assert registry.removed == []

    def test_an_outdoor_entity_is_left_alone(self, loaded):
        from homeassistant.helpers import entity_registry as er

        hass, entry, _devices = loaded
        registry = er.async_get(hass)
        registry.add("sensor.cu_s_01_compressor",
                     f"{entry.entry_id}_system_S-01_compressor")

        report = call(hass, "purge_orphans", {"dry_run": False})
        assert registry.removed == []

    def test_another_integrations_entity_is_left_alone(self, loaded):
        from homeassistant.helpers import entity_registry as er

        hass, entry, _devices = loaded
        registry = er.async_get(hass)
        registry.add("sensor.something_else", "not_ours_at_all")

        report = call(hass, "purge_orphans", {"dry_run": False})
        assert registry.removed == []

    def test_it_can_be_limited_to_one_gateway(self, loaded):
        hass, entry, _devices = loaded
        self._orphan(hass, entry)

        report = call(hass, "purge_orphans",
                      {"dry_run": True, "host": "192.0.2.99"})
        assert report["orphans"] == []


class TestRecheck:
    def test_it_reports_each_gateway(self, loaded):
        hass, entry, _devices = loaded
        report = call(hass, "recheck", {})
        assert len(report["gateways"]) == 1
        assert report["gateways"][0]["gateway"] == entry.title
        assert report["gateways"][0]["ok"] is True

    def test_another_host_is_left_alone(self, loaded):
        hass, _entry, _devices = loaded
        assert call(hass, "recheck", {"host": "198.51.100.1"}) == {"gateways": []}


class TestOutdoorUnits:
    def _outdoor(self, registry, entry):
        return next(
            device for device in registry.devices.values()
            if (DOMAIN, f"{entry.entry_id}_system_S-01") in device.identifiers
        )

    def test_registered_at_setup_under_the_gateway(self, loaded):
        hass, entry, registry = loaded
        outdoor = self._outdoor(registry, entry)
        assert outdoor.via_device_id == "hub_device_id"
        assert entry.runtime_data.system_device_ids == {"S-01": outdoor.id}

    def test_placed_in_the_mappings_outdoor_area(self, loaded):
        hass, entry, registry = loaded
        outdoor = self._outdoor(registry, entry)
        assert outdoor.area_id == "plant_deck"

    def test_an_area_set_by_hand_is_kept(self, loaded):
        from custom_components.daikin_dbacs import _register_outdoor_units

        hass, entry, registry = loaded
        outdoor = self._outdoor(registry, entry)
        outdoor.area_id = "somewhere_else"
        _register_outdoor_units(hass, entry, entry.runtime_data, registry)
        assert outdoor.area_id == "somewhere_else"

    def test_indoor_units_hang_off_the_outdoor_unit_serving_them(self, loaded):
        from custom_components.daikin_dbacs.entity import DbacsEntity
        from custom_components.daikin_dbacs.objects import MEMBERS_BY_KEY

        hass, entry, registry = loaded
        coordinator = entry.runtime_data
        unit = DbacsEntity(coordinator, 0, MEMBERS_BY_KEY["room_temp"])
        assert unit._attr_device_info["via_device_id"] == self._outdoor(registry, entry).id


def test_a_unit_with_a_device_is_watched_even_if_dead_at_setup(monkeypatch, site):
    """Found before (it has a device), dead now: still owed its alarm."""
    from custom_components.daikin_dbacs.const import DATA_KNOWN_GROUPS

    state = fake.FakeGatewayState(populated={0, 1}, dead={1}, device_instance=12)

    async def connect(self):
        fake.attach(self, state)
        return await self.read_info()

    monkeypatch.setattr(gw.DbacsGateway, "connect", connect)
    hass = HomeAssistant()
    entry = ConfigEntry(
        data={CONF_HOST: "192.0.2.10", CONF_DEVICE_INSTANCE: 12},
        options={OPT_MEMBERS: MEMBERS}, entry_id="entry_twelve",
    )
    hass.config_entries.entries.append(entry)
    dr.async_get(hass).add_device(
        "old_device", {(DOMAIN, "entry_twelve_1")}, config_entry_id="entry_twelve")
    run(async_setup_entry(hass, entry))
    coordinator = entry.runtime_data
    assert 1 not in coordinator.active_groups
    assert coordinator.watched_groups == (0, 1)
    assert entry.data[DATA_KNOWN_GROUPS] == [0, 1]
