"""What the platforms create, and when an entity admits it does not know."""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.daikin_dbacs import alarms, binary_sensor, sensor
from custom_components.daikin_dbacs import gateway as gw
from custom_components.daikin_dbacs.const import OPT_MEMBERS
from custom_components.daikin_dbacs.coordinator import DbacsCoordinator

import fake_gateway as fake

ALARMS = (alarms.UnitNotResponding, alarms.SystemNotResponding, alarms.UnitsInAlarm)


def run(coro):
    return asyncio.run(coro)


def build(state=None, options=None):
    state = state or fake.FakeGatewayState(populated={0, 1})
    client = gw.DbacsGateway("192.0.2.10")
    fake.attach(client, state)
    info = run(client.read_info())
    entry = ConfigEntry(data={"host": "192.0.2.10"}, options=options or {})
    coordinator = DbacsCoordinator(HomeAssistant(), entry, client, info)
    run(coordinator.async_discover())
    run(coordinator.async_refresh())
    entry.runtime_data = coordinator
    return coordinator, entry, state


def collect(module, entry):
    """The point entities a platform creates (the alarms: test_alarms.py)."""
    created: list = []
    run(
        module.async_setup_entry(
            HomeAssistant(), entry, lambda entities: created.extend(entities)
        )
    )
    return [e for e in created if not isinstance(e, ALARMS)]


class TestWhatGetsCreated:
    def test_every_live_group_gets_entities(self):
        _coordinator, entry, _state = build()
        sensors = collect(sensor, entry)
        binaries = collect(binary_sensor, entry)
        assert {e.ac_number for e in sensors + binaries} == {0, 1}

    def test_a_dead_group_gets_none(self):
        state = fake.FakeGatewayState(populated={0, 1}, dead={1})
        _coordinator, entry, _state = build(state)
        created = collect(sensor, entry) + collect(binary_sensor, entry)
        assert {e.ac_number for e in created} == {0}

    def test_the_two_platforms_divide_the_points_between_them(self):
        _coordinator, entry, _state = build(
            fake.FakeGatewayState(populated={0})
        )
        sensor_keys = {e.member.key for e in collect(sensor, entry)}
        binary_keys = {e.member.key for e in collect(binary_sensor, entry)}
        assert not sensor_keys & binary_keys

    def test_command_objects_are_not_exposed_as_readings(self):
        # A command object reports what was last asked of it, not what the
        # unit is doing. Showing it as a sensor would read as truth.
        _coordinator, entry, _state = build(
            fake.FakeGatewayState(populated={0})
        )
        created = collect(sensor, entry) + collect(binary_sensor, entry)
        keys = {e.member.key for e in created}
        assert "mode_command" not in keys
        assert "on_off_command" not in keys
        assert "system_forced_off" not in keys
        assert "mode_status" in keys
        assert "on_off_status" in keys

    def test_nothing_write_only_is_exposed(self):
        _coordinator, entry, _state = build(
            fake.FakeGatewayState(populated={0})
        )
        created = collect(sensor, entry) + collect(binary_sensor, entry)
        assert "filter_sign_reset" not in {e.member.key for e in created}

    def test_points_not_chosen_exist_but_start_disabled(self):
        # They are in the registry so Home Assistant's own entity list can
        # bulk-enable them; they are simply never read until it does.
        _coordinator, entry, _state = build(
            fake.FakeGatewayState(populated={0}),
            options={OPT_MEMBERS: ["room_temp"]},
        )
        created = collect(sensor, entry) + collect(binary_sensor, entry)
        by_key = {e.member.key: e for e in created}
        assert by_key["room_temp"].entity_registry_enabled_default
        assert not by_key["heater_status"].entity_registry_enabled_default


class TestIdentity:
    def test_unique_ids_do_not_depend_on_names(self):
        _coordinator, entry, _state = build()
        created = collect(sensor, entry) + collect(binary_sensor, entry)
        ids = [e.unique_id for e in created]
        assert len(ids) == len(set(ids))
        assert "test_entry_0_room_temp" in ids

    def test_devices_are_named_by_their_diii_address(self):
        _coordinator, entry, _state = build()
        entity = next(
            e for e in collect(sensor, entry)
            if e.ac_number == 1 and e.member.key == "room_temp"
        )
        assert entity.device_info["name"] == "DBACS 1 · 1-01"
        assert entity.device_info["serial_number"] == "D3 1-1-01"

    def test_one_device_per_group(self):
        _coordinator, entry, _state = build()
        created = collect(sensor, entry) + collect(binary_sensor, entry)
        identifiers = {
            tuple(sorted(e.device_info["identifiers"])) for e in created
        }
        assert len(identifiers) == 2


class TestValues:
    def test_a_temperature_arrives_as_a_number(self):
        _coordinator, entry, _state = build()
        entity = _find(collect(sensor, entry), 0, "room_temp")
        assert entity.native_value == pytest.approx(20.0)
        assert entity.native_unit_of_measurement == "°C"

    def test_an_enumeration_arrives_labelled(self):
        _coordinator, entry, _state = build()
        entity = _find(collect(sensor, entry), 1, "fan_status")
        # Group 1 reports value 2, which Daikin defines as High.
        assert entity.native_value == "High"

    def test_enum_options_leave_out_the_state_that_never_arrives(self):
        _coordinator, entry, _state = build()
        entity = _find(collect(sensor, entry), 0, "mode_status")
        assert entity._attr_options == ["Cool", "Heat", "Fan", "Dry"]

    def test_a_binary_point_is_on_or_off(self):
        _coordinator, entry, _state = build()
        entity = _find(collect(binary_sensor, entry), 1, "on_off_status")
        assert entity.is_on is True
        assert _find(collect(binary_sensor, entry), 0, "on_off_status").is_on is False

    def test_a_fault_carries_its_daikin_code(self):
        state = fake.FakeGatewayState(populated={0}, faulted={0})
        _coordinator, entry, _state = build(state)
        entity = _find(collect(sensor, entry), 0, "malfunction_code")
        assert entity.extra_state_attributes["fault_code"] == "U1"

    def test_every_entity_says_which_object_it_reads(self):
        _coordinator, entry, _state = build()
        entity = _find(collect(sensor, entry), 1, "room_temp")
        attributes = entity.extra_state_attributes
        assert attributes["bacnet_object"] == "RoomTemp_001"
        assert attributes["bacnet_instance"] == "265"
        assert attributes["diii_address"] == "1 · 1-01"


class TestAvailability:
    def test_a_point_with_an_answer_is_available(self):
        _coordinator, entry, _state = build()
        assert _find(collect(sensor, entry), 0, "room_temp").available

    def test_a_point_that_errored_is_unavailable_not_stale(self):
        # A confident number from a dead point is the one failure worth
        # designing against.
        state = fake.FakeGatewayState(populated={0})
        coordinator, entry, _state = build(state)
        entity = _find(collect(sensor, entry), 0, "room_temp")
        assert entity.native_value is not None

        coordinator.data[entity.point.key] = gw.PointValue(
            entity.point, error="unknown-object"
        )
        assert not entity.available
        assert entity.native_value is None

    def test_a_point_never_answered_is_unavailable(self):
        coordinator, entry, _state = build()
        entity = _find(collect(sensor, entry), 0, "accumulated_power")
        coordinator.data.pop(entity.point.key, None)
        assert not entity.available

    def test_everything_goes_unavailable_when_the_sweep_fails(self):
        coordinator, entry, _state = build()
        entity = _find(collect(sensor, entry), 0, "room_temp")
        coordinator.last_update_success = False
        assert not entity.available

    def test_a_group_that_stops_communicating_goes_unavailable(self):
        state = fake.FakeGatewayState(populated={0, 1})
        coordinator, entry, _state = build(state)
        entity = _find(collect(sensor, entry), 1, "room_temp")
        assert entity.available

        state.dead.add(1)
        run(coordinator.async_refresh())
        assert not entity.available

    def test_an_error_is_reported_on_the_entity(self):
        coordinator, entry, _state = build()
        entity = _find(collect(sensor, entry), 0, "room_temp")
        coordinator.data[entity.point.key] = gw.PointValue(
            entity.point, error="property/unknown-property"
        )
        assert entity.extra_state_attributes["error"] == (
            "property/unknown-property"
        )


def _find(entities, ac_number, member_key):
    return next(
        e
        for e in entities
        if e.ac_number == ac_number and e.member.key == member_key
    )


class TestPresentation:
    def test_a_temperature_is_rounded(self):
        # The gateway sends float32, so a reading arrives as
        # 27.2000007629395. The raw value is what gets recorded, so it is
        # rounded here rather than only in the display precision.
        coordinator, entry, _state = build()
        entity = _find(collect(sensor, entry), 0, "room_temp")
        coordinator.data[entity.point.key] = gw.PointValue(
            entity.point, value=27.2000007629395
        )
        assert entity.native_value == 27.2

    def test_a_healthy_unit_reads_normal_not_a_number(self):
        coordinator, entry, _state = build(
            fake.FakeGatewayState(populated={0})
        )
        entity = _find(collect(sensor, entry), 0, "malfunction_code")
        assert entity.native_value == "Normal"

    def test_a_faulted_unit_reads_its_daikin_code(self):
        # The object's value is an index into a table Daikin publishes
        # separately. The two-character code is what an engineer needs.
        state = fake.FakeGatewayState(populated={0}, faulted={0})
        coordinator, entry, _state = build(state)
        entity = _find(collect(sensor, entry), 0, "malfunction_code")
        assert entity.native_value == "U1"
        assert entity.extra_state_attributes["raw_value"] == "42"

    def test_a_fault_code_carries_no_enum_options(self):
        coordinator, entry, _state = build(
            fake.FakeGatewayState(populated={0})
        )
        entity = _find(collect(sensor, entry), 0, "malfunction_code")
        assert entity.options is None
        assert entity.device_class is None


class TestNoCommandIsEverASensor:
    def test_an_analog_command_is_not_published_as_a_sensor(self):
        # AirDirectionCommand is an analog value, and the earlier filter
        # only excluded binary and multistate commands — so it appeared as a
        # sensor on all 96 groups, reading what was last asked for rather
        # than what the unit is doing.
        _coordinator, entry, _state = build(
            fake.FakeGatewayState(populated={0})
        )
        keys = {e.member.key for e in collect(sensor, entry)}
        assert "air_direction_command" not in keys
        assert "air_direction_status" in keys

    def test_no_command_member_reaches_either_platform(self):
        from custom_components.daikin_dbacs.objects import Kind

        _coordinator, entry, _state = build(
            fake.FakeGatewayState(populated={0})
        )
        created = collect(sensor, entry) + collect(binary_sensor, entry)
        assert [e.member.key for e in created if e.member.kind is Kind.COMMAND] == []
