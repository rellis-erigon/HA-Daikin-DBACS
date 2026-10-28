"""A unit that was here and has gone quiet, or is not Normal, is an alarm."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.daikin_dbacs import alarms, binary_sensor
from custom_components.daikin_dbacs import gateway as gw
from custom_components.daikin_dbacs.const import DATA_KNOWN_GROUPS
from custom_components.daikin_dbacs.coordinator import DbacsCoordinator
from custom_components.daikin_dbacs.mapping import UnitMapping
from custom_components.daikin_dbacs.systems import systems_from_mapping

import fake_gateway as fake


def run(coro):
    return asyncio.run(coro)


def Unit(name, system, outdoor):
    return UnitMapping(0, name=name, system=system, outdoor=outdoor)


def build(state, data=None):
    client = gw.DbacsGateway("192.0.2.10")
    fake.attach(client, state)
    info = run(client.read_info())
    entry = ConfigEntry(data={"host": "192.0.2.10", **(data or {})}, options={})
    coordinator = DbacsCoordinator(HomeAssistant(), entry, client, info)
    coordinator.site = {
        0: Unit("Unit A", "S1", "CU-1"),
        1: Unit("Unit B", "S1", "CU-1"),
        2: Unit("Unit C", "S2", "CU-2"),
    }
    coordinator.systems = systems_from_mapping(coordinator.site)
    run(coordinator.async_discover())
    run(coordinator.async_refresh())
    entry.runtime_data = coordinator
    return coordinator, entry


def entities(entry):
    created: list = []
    run(binary_sensor.async_setup_entry(
        HomeAssistant(), entry, lambda batch: created.extend(batch)))
    return created


def one(created, cls, **match):
    return next(e for e in created if isinstance(e, cls)
                and all(getattr(e, k) == v for k, v in match.items()))


class TestRemembering:
    def test_units_found_are_remembered_in_the_entry(self):
        coordinator, entry = build(fake.FakeGatewayState(populated={0, 1, 2}))
        assert entry.data[DATA_KNOWN_GROUPS] == [0, 1, 2]
        assert coordinator.watched_groups == (0, 1, 2)

    def test_a_unit_dead_when_home_assistant_starts_is_still_watched(self):
        # Found before a restart, dead after it: still this gateway's unit.
        state = fake.FakeGatewayState(populated={0, 1, 2}, dead={1})
        coordinator, _ = build(state, {DATA_KNOWN_GROUPS: [0, 1, 2]})
        assert 1 not in coordinator.active_groups
        assert coordinator.watched_groups == (0, 1, 2)
        assert coordinator.not_responding(1) == "DIII-NET communication fault"

    def test_a_unit_never_seen_is_not_an_alarm(self):
        coordinator, _ = build(fake.FakeGatewayState(populated={0, 1}, dead={1}))
        assert coordinator.watched_groups == (0,)

    def test_forgetting_drops_only_units_the_gateway_no_longer_has(self):
        state = fake.FakeGatewayState(populated={0, 1}, dead={1})
        coordinator, entry = build(state, {DATA_KNOWN_GROUPS: [0, 1, 2]})
        assert coordinator.forget_missing() == [2]
        assert entry.data[DATA_KNOWN_GROUPS] == [0, 1]


class TestIndoorUnit:
    def test_quiet_while_answering(self):
        coordinator, entry = build(fake.FakeGatewayState(populated={0, 1, 2}))
        alarm = one(entities(entry), alarms.UnitNotResponding, ac_number=1)
        assert alarm.available and alarm.is_on is False

    def test_a_unit_that_stops_communicating_raises_it(self):
        state = fake.FakeGatewayState(populated={0, 1, 2})
        coordinator, entry = build(state)
        alarm = one(entities(entry), alarms.UnitNotResponding, ac_number=1)
        state.dead.add(1)
        run(coordinator.async_refresh())
        # On, not unavailable: the alarm must not go grey with its unit.
        assert alarm.available and alarm.is_on is True
        assert alarm.extra_state_attributes["reason"] == "DIII-NET communication fault"

    def test_a_unit_whose_reads_all_fail_raises_it(self):
        state = fake.FakeGatewayState(populated={0, 1, 2})
        coordinator, _ = build(state)
        # Every point the unit has, the communication status included.
        state.broken.update(
            fake.expected_instance(1, key.split("_", 1)[1])
            for key in coordinator.data if key.startswith("1_"))
        run(coordinator.async_refresh())
        assert coordinator.not_responding(1) == "not answering reads"
        assert coordinator.not_responding(0) is None

    def test_the_gateway_going_quiet_raises_every_one(self):
        coordinator, _ = build(fake.FakeGatewayState(populated={0, 1, 2}))
        coordinator.last_update_success = False
        assert all(coordinator.not_responding(ac) == "gateway not answering"
                   for ac in (0, 1, 2))


class TestOutdoorUnit:
    def test_one_indoor_unit_quiet(self):
        state = fake.FakeGatewayState(populated={0, 1, 2})
        coordinator, entry = build(state)
        alarm = one(entities(entry), alarms.SystemNotResponding, system_name="S1")
        state.dead.add(1)
        run(coordinator.async_refresh())
        assert alarm.is_on is True
        attrs = alarm.extra_state_attributes
        assert attrs["all_not_responding"] is False
        assert attrs["units_not_responding"] == [
            "Unit B (1 · 1-01): DIII-NET communication fault"]

    def test_every_indoor_unit_quiet_points_at_the_outdoor_unit(self):
        state = fake.FakeGatewayState(populated={0, 1, 2})
        coordinator, entry = build(state)
        alarm = one(entities(entry), alarms.SystemNotResponding, system_name="S1")
        state.dead.update({0, 1})
        run(coordinator.async_refresh())
        assert alarm.available and alarm.is_on is True
        assert alarm.extra_state_attributes["all_not_responding"] is True
        summary = one(entities(entry), alarms.UnitsInAlarm)
        assert summary.extra_state_attributes["outdoor_units"] == [
            "CU-1: none of its 2 indoor units answering "
            "(the outdoor unit, its power or its wiring)"]


class TestGateway:
    def test_all_normal(self):
        coordinator, entry = build(fake.FakeGatewayState(populated={0, 1, 2}))
        summary = one(entities(entry), alarms.UnitsInAlarm)
        assert summary.is_on is False
        assert summary.extra_state_attributes["units_watched"] == 3

    def test_a_unit_not_normal_is_in_alarm(self):
        state = fake.FakeGatewayState(populated={0, 1, 2}, faulted={2})
        coordinator, entry = build(state)
        summary = one(entities(entry), alarms.UnitsInAlarm)
        assert summary.is_on is True
        attrs = summary.extra_state_attributes
        assert attrs["in_malfunction"] == 1
        # The code, with what Daikin says it means.
        assert attrs["units_in_malfunction"] == [
            "Unit C (1 · 1-02): U1 (power supply reversed or lost phase)"]
        assert attrs["outdoor_units"][0].startswith("CU-2: ")

    def test_a_unit_not_responding_is_in_alarm(self):
        state = fake.FakeGatewayState(populated={0, 1, 2})
        coordinator, entry = build(state)
        summary = one(entities(entry), alarms.UnitsInAlarm)
        state.dead.add(0)
        run(coordinator.async_refresh())
        assert summary.is_on is True
        assert summary.extra_state_attributes["units_not_responding"] == [
            "Unit A (1 · 1-00): DIII-NET communication fault"]


def test_remembering_a_unit_does_not_reload_the_entry():
    # The update listener reloads on any entry change; a remembered unit is
    # a data change, and reloading for it would loop at every setup.
    from custom_components.daikin_dbacs import _async_options_updated

    coordinator, entry = build(fake.FakeGatewayState(populated={0, 1, 2}))
    hass = coordinator.hass
    run(_async_options_updated(hass, entry))
    assert hass.config_entries.reloaded == []
    entry.options = {"scan_interval": 30}
    run(_async_options_updated(hass, entry))
    assert hass.config_entries.reloaded == [entry.entry_id]


def test_codes_carry_their_meaning_and_unknown_ones_are_not_guessed():
    from custom_components.daikin_dbacs.codes import describe, meaning

    assert describe("E5") == "E5 (inverter compressor locked or overloaded)"
    assert meaning("u4") == "indoor and outdoor units not communicating"
    assert describe("Z9") == "Z9"
    assert describe(None) is None
