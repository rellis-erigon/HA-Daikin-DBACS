"""Setting the entry up and tearing it down."""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.daikin_dbacs import (
    async_setup_entry,
    async_unload_entry,
    diagnostics,
)
from custom_components.daikin_dbacs import gateway as gw
from custom_components.daikin_dbacs.const import CONF_HOST, OPT_MEMBERS

import fake_gateway as fake


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def connects(monkeypatch):
    state = fake.FakeGatewayState(populated={0, 1, 64}, faulted={1})

    async def connect(self):
        fake.attach(self, state)
        return await self.read_info()

    monkeypatch.setattr(gw.DbacsGateway, "connect", connect)
    return state


def entry_for(options=None):
    return ConfigEntry(
        data={CONF_HOST: "192.0.2.10"},
        options=options or {OPT_MEMBERS: ["room_temp", "alarm", "malfunction_code"]},
    )


class TestSetup:
    def test_sets_up_and_discovers(self, connects):
        hass = HomeAssistant()
        entry = entry_for()
        assert run(async_setup_entry(hass, entry))

        coordinator = entry.runtime_data
        assert coordinator.active_groups == (0, 1, 64)
        assert coordinator.data
        assert hass.config_entries.forwarded

    def test_an_unreachable_gateway_defers_rather_than_half_loading(
        self, monkeypatch
    ):
        async def connect(self):
            raise gw.DbacsUnreachable("no answer")

        monkeypatch.setattr(gw.DbacsGateway, "connect", connect)
        with pytest.raises(ConfigEntryNotReady):
            run(async_setup_entry(HomeAssistant(), entry_for()))

    def test_a_gateway_still_scanning_defers_too(self, monkeypatch):
        async def connect(self):
            raise gw.DbacsNotReady("configuration-in-progress")

        monkeypatch.setattr(gw.DbacsGateway, "connect", connect)
        # Home Assistant will retry, which is exactly the right behaviour for
        # a gateway that is simply not finished booting.
        with pytest.raises(ConfigEntryNotReady):
            run(async_setup_entry(HomeAssistant(), entry_for()))

    def test_unloading_gives_the_gateway_its_slots_back(self, connects):
        hass = HomeAssistant()
        entry = entry_for()
        run(async_setup_entry(hass, entry))
        coordinator = entry.runtime_data

        assert run(async_unload_entry(hass, entry))
        assert not coordinator.gateway.connected
        assert coordinator.gateway.subscription_count == 0


class TestDiagnostics:
    def test_tells_a_quiet_gateway_from_an_empty_one(self, connects):
        hass = HomeAssistant()
        entry = entry_for()
        run(async_setup_entry(hass, entry))

        report = run(
            diagnostics.async_get_config_entry_diagnostics(hass, entry)
        )

        assert report["gateway"]["model_name"] == "DMS502B71"
        assert report["gateway"]["max_apdu_length"] == 1024
        assert report["discovery"]["live_count"] == 3
        assert report["discovery"]["live"] == [
            "1 · 1-00",
            "1 · 1-01",
            "2 · 1-00",
        ]
        assert report["discovery"]["absent_count"] == 253
        assert report["points"]["requested"] == 9
        assert report["points"]["errored"] == 0
        assert report["fault_codes"] == {"1 · 1-01": "U1"}

    def test_reports_dead_groups_separately_from_absent_ones(
        self, monkeypatch
    ):
        state = fake.FakeGatewayState(populated={0, 1}, dead={1})

        async def connect(self):
            fake.attach(self, state)
            return await self.read_info()

        monkeypatch.setattr(gw.DbacsGateway, "connect", connect)
        hass = HomeAssistant()
        entry = entry_for()
        run(async_setup_entry(hass, entry))

        report = run(
            diagnostics.async_get_config_entry_diagnostics(hass, entry)
        )
        assert report["discovery"]["dead"] == ["1 · 1-01"]
        assert report["discovery"]["live"] == ["1 · 1-00"]


class TestTheGatewayIsADevice:
    def test_the_gateway_itself_is_registered(self, connects):
        # The 96 group devices need something real to hang off, and
        # via_device_id has to point at a device that exists rather than at
        # an identifier nothing ever created.
        from homeassistant.helpers import device_registry as dr

        hass = HomeAssistant()
        entry = entry_for()
        run(async_setup_entry(hass, entry))

        created = dr.async_get(hass).created
        assert len(created) == 1
        hub = created[0]
        assert hub["name"] == "D-BACS-TEST"
        assert hub["model"] == "DMS502B71"
        assert hub["manufacturer"] == "Daikin Industries"
        assert entry.runtime_data.hub_device_id == "hub_device_id"

    def test_groups_hang_off_the_gateway(self, connects):
        from custom_components.daikin_dbacs import sensor

        hass = HomeAssistant()
        entry = entry_for()
        run(async_setup_entry(hass, entry))

        created: list = []
        run(
            sensor.async_setup_entry(
                hass, entry, lambda entities: created.extend(entities)
            )
        )
        assert created
        for entity in created:
            assert entity.device_info["via_device_id"] == "hub_device_id"
            assert "via_device" not in entity.device_info


class TestThePointSelectionIsHonoured:
    """Adding a point in the options has to actually enable its entities.

    `entity_registry_enabled_default` only applies when an entity is first
    created, so without reconciling, a point switched on later stayed
    disabled from a previous run and was never read again — which made the
    bulk enable and disable this integration advertises do nothing after the
    first setup.
    """

    def _registry(self, hass, entry):
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        registry.entries.clear()
        return registry, er

    def test_a_point_switched_on_later_is_re_enabled(self, connects):
        from custom_components.daikin_dbacs import async_setup_entry as setup

        hass = HomeAssistant()
        entry = entry_for({OPT_MEMBERS: ["room_temp", "heater_status"]})
        registry, er = self._registry(hass, entry)
        # As a previous run left it: we disabled it, because it was not chosen.
        registry.add(
            "binary_sensor.dbacs_heater",
            f"{entry.entry_id}_0_heater_status",
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )

        run(setup(hass, entry))
        assert registry.entries[0].disabled_by is None

    def test_a_point_switched_off_is_disabled(self, connects):
        from custom_components.daikin_dbacs import async_setup_entry as setup

        hass = HomeAssistant()
        entry = entry_for({OPT_MEMBERS: ["room_temp"]})
        registry, _er = self._registry(hass, entry)
        registry.add(
            "binary_sensor.dbacs_heater", f"{entry.entry_id}_0_heater_status"
        )

        run(setup(hass, entry))
        assert registry.entries[0].disabled_by == "integration"

    def test_a_point_switched_off_loses_its_stale_state(self, connects):
        # Home Assistant publishes a restored state for the entity before
        # setup gets here, and disabling the registry entry does not take it
        # away. Without removing it, narrowing the point list leaves
        # hundreds of entities reading `unavailable` until the whole of Home
        # Assistant restarts.
        from custom_components.daikin_dbacs import async_setup_entry as setup

        hass = HomeAssistant()
        entry = entry_for({OPT_MEMBERS: ["room_temp"]})
        registry, _er = self._registry(hass, entry)
        registry.add(
            "binary_sensor.dbacs_heater", f"{entry.entry_id}_0_heater_status"
        )

        run(setup(hass, entry))
        assert hass.states.removed == ["binary_sensor.dbacs_heater"]

    def test_a_point_left_on_keeps_its_state(self, connects):
        from custom_components.daikin_dbacs import async_setup_entry as setup

        hass = HomeAssistant()
        entry = entry_for({OPT_MEMBERS: ["room_temp"]})
        registry, _er = self._registry(hass, entry)
        registry.add("sensor.dbacs_room_temp", f"{entry.entry_id}_0_room_temp")

        run(setup(hass, entry))
        assert hass.states.removed == []

    def test_a_point_switched_off_by_hand_stays_off(self, connects):
        from custom_components.daikin_dbacs import async_setup_entry as setup

        hass = HomeAssistant()
        entry = entry_for({OPT_MEMBERS: ["room_temp"]})
        registry, er = self._registry(hass, entry)
        registry.add(
            "sensor.dbacs_room_temp",
            f"{entry.entry_id}_0_room_temp",
            disabled_by=er.RegistryEntryDisabler.USER,
        )

        run(setup(hass, entry))
        # Chosen in the options, but somebody turned it off deliberately.
        assert registry.entries[0].disabled_by == "user"

    def test_the_climate_entity_is_not_treated_as_a_point(self, connects):
        from custom_components.daikin_dbacs import async_setup_entry as setup

        hass = HomeAssistant()
        entry = entry_for({OPT_MEMBERS: ["room_temp"]})
        registry, _er = self._registry(hass, entry)
        registry.add("climate.dbacs_0", f"{entry.entry_id}_0_climate")
        registry.add("event.dbacs_0_fault", f"{entry.entry_id}_0_fault_event")

        run(setup(hass, entry))
        assert all(e.disabled_by is None for e in registry.entries)

    def test_another_integrations_entity_is_left_alone(self, connects):
        from custom_components.daikin_dbacs import async_setup_entry as setup

        hass = HomeAssistant()
        entry = entry_for({OPT_MEMBERS: ["room_temp"]})
        registry, _er = self._registry(hass, entry)
        registry.add("sensor.something_else", "some_other_unique_id")

        run(setup(hass, entry))
        assert registry.updates == []
