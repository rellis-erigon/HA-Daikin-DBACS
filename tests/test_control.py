"""Phase 2: the climate entity, and what stands between it and the gateway."""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.daikin_dbacs import button, climate, event, switch
from custom_components.daikin_dbacs import gateway as gw
from custom_components.daikin_dbacs.const import (
    OPT_CONTROL,
    OPT_GATED,
    OPT_MEMBERS,
    OPT_MIN_WRITE_INTERVAL,
    OPT_WRITE_PRIORITY,
)
from custom_components.daikin_dbacs.coordinator import DbacsCoordinator
from custom_components.daikin_dbacs.objects import MEMBERS_BY_KEY, PointRef, READABLE

import fake_gateway as fake


def run(coro):
    return asyncio.run(coro)


ALL_MEMBERS = [m.key for m in READABLE]


def build(state=None, options=None, control=True):
    state = state or fake.FakeGatewayState(populated={0, 1})
    client = gw.DbacsGateway("192.0.2.10")
    fake.attach(client, state)
    info = run(client.read_info())
    opts = {OPT_MEMBERS: ALL_MEMBERS, OPT_CONTROL: control}
    opts.update(options or {})
    entry = ConfigEntry(data={"host": "192.0.2.10"}, options=opts)
    hass = HomeAssistant()
    coordinator = DbacsCoordinator(hass, entry, client, info)
    run(coordinator.async_discover())
    run(coordinator.async_refresh())
    entry.runtime_data = coordinator
    return coordinator, entry, state, hass


def collect(module, entry, hass=None):
    created: list = []
    run(
        module.async_setup_entry(
            hass or HomeAssistant(), entry, lambda e: created.extend(e)
        )
    )
    return created


def find(entities, ac_number):
    return next(e for e in entities if e.ac_number == ac_number)


class TestControlIsOptIn:
    def test_no_climate_entity_until_control_is_on(self):
        # An update must not quietly make a building writable.
        _c, entry, _s, _h = build(control=False)
        assert collect(climate, entry) == []
        assert collect(switch, entry) == []
        assert collect(button, entry) == []

    def test_climate_entities_appear_once_it_is(self):
        _c, entry, _s, _h = build(control=True)
        assert len(collect(climate, entry)) == 2

    def test_a_write_with_control_off_is_refused_with_a_reason(self):
        coordinator, _entry, state, _h = build(control=False)
        point = PointRef(0, MEMBERS_BY_KEY["on_off_command"])
        with pytest.raises(gw.DbacsError.__mro__[1]):  # WriteRefused
            run(coordinator.async_write(point, "active"))
        assert state.writes == []

    def test_fault_events_do_not_need_control(self):
        # Reading when something broke is not a control function.
        _c, entry, _s, _h = build(control=False)
        assert len(collect(event, entry)) == 2


class TestReadingTheThermostat:
    def test_a_stopped_unit_reads_off(self):
        # Group 0 reports StartStopStatus inactive in the synthetic gateway.
        _c, entry, _s, _h = build()
        assert find(collect(climate, entry), 0).hvac_mode is HVACMode.OFF

    def test_a_running_unit_reads_its_mode(self):
        _c, entry, _s, _h = build()
        # Group 1 is running, mode 2.
        assert find(collect(climate, entry), 1).hvac_mode is HVACMode.HEAT

    def test_mode_enumeration_matches_daikin(self):
        assert climate.MODE_TO_HVAC == {
            1: HVACMode.COOL,
            2: HVACMode.HEAT,
            3: HVACMode.FAN_ONLY,
            4: HVACMode.AUTO,
            5: HVACMode.DRY,
        }

    def test_fan_modes_are_presented_in_human_order(self):
        # Daikin's enumeration is Low, High, Middle, Auto. Home Assistant is
        # shown low, medium, high and the mapping does the reordering.
        assert climate.FAN_TO_VALUE == {"low": 1, "medium": 3, "high": 2, "auto": 4}
        entity = find(collect(climate, build()[1]), 1)
        assert entity.fan_modes == ["auto", "low", "medium", "high"]

    def test_fan_mode_reads_through_the_reordering(self):
        _c, entry, _s, _h = build()
        # Group 1 reports airflow 2, which Daikin defines as High.
        assert find(collect(climate, entry), 1).fan_mode == "high"

    def test_a_stopped_unit_is_not_doing_anything(self):
        _c, entry, _s, _h = build()
        assert find(collect(climate, entry), 0).hvac_action is HVACAction.OFF

    def test_a_running_unit_with_the_thermostat_off_is_idle(self):
        coordinator, entry, _s, _h = build()
        entity = find(collect(climate, entry), 1)
        # The synthetic gateway reports thermostat off.
        assert entity.hvac_action is HVACAction.IDLE

    def test_a_calling_unit_reports_what_it_is_doing(self):
        coordinator, entry, _s, _h = build()
        entity = find(collect(climate, entry), 1)
        coordinator.data["1_thermo_status"] = gw.PointValue(
            PointRef(1, MEMBERS_BY_KEY["thermo_status"]), value=1
        )
        assert entity.hvac_action is HVACAction.HEATING

    def test_swing_is_read_from_a_non_contiguous_enumeration(self):
        coordinator, entry, _s, _h = build()
        entity = find(collect(climate, entry), 1)
        point = PointRef(1, MEMBERS_BY_KEY["air_direction_status"])
        for raw, expected in ((0.0, "horizontal"), (3.0, "horizontal"),
                              (4.0, "vertical"), (7.0, "both")):
            coordinator.data[point.key] = gw.PointValue(point, value=raw)
            assert entity.swing_mode == expected

    def test_temperatures_are_rounded(self):
        coordinator, entry, _s, _h = build()
        entity = find(collect(climate, entry), 1)
        point = PointRef(1, MEMBERS_BY_KEY["room_temp"])
        coordinator.data[point.key] = gw.PointValue(
            point, value=27.2000007629395
        )
        assert entity.current_temperature == 27.2

    def test_a_unit_that_cannot_be_read_is_unavailable(self):
        coordinator, entry, _s, _h = build()
        entity = find(collect(climate, entry), 1)
        assert entity.available
        coordinator.data.pop("1_on_off_status")
        assert not entity.available


class TestWriting:
    def test_setting_a_mode_sends_the_mode_then_the_power(self):
        # A unit told to run before it is told what to do runs in whatever
        # mode it was last left in, which is visible in the room.
        coordinator, entry, state, _h = build()
        entity = find(collect(climate, entry), 0)
        run(entity.async_set_hvac_mode(HVACMode.COOL))
        members = [instance % 256 for _t, instance, _v, _p in state.writes]
        assert members == [
            MEMBERS_BY_KEY["mode_command"].number,
            MEMBERS_BY_KEY["on_off_command"].number,
        ]
        assert state.writes[0][2] == 1

    def test_turning_off_writes_only_the_power(self):
        coordinator, entry, state, _h = build()
        entity = find(collect(climate, entry), 1)
        run(entity.async_set_hvac_mode(HVACMode.OFF))
        assert len(state.writes) == 1
        assert state.writes[0][2] == "inactive"

    def test_a_running_unit_is_not_told_to_run_again(self):
        coordinator, entry, state, _h = build()
        entity = find(collect(climate, entry), 1)  # already running
        run(entity.async_set_hvac_mode(HVACMode.DRY))
        assert len(state.writes) == 1
        assert state.writes[0][2] == 5

    def test_a_setpoint_is_written_as_a_number(self):
        coordinator, entry, state, _h = build()
        entity = find(collect(climate, entry), 0)
        run(entity.async_set_temperature(temperature=21.5))
        assert state.writes[0][2] == 21.5

    def test_a_fan_speed_is_written_through_the_reordering(self):
        coordinator, entry, state, _h = build()
        entity = find(collect(climate, entry), 0)
        run(entity.async_set_fan_mode("medium"))
        # "medium" is Daikin's 3, not its 2.
        assert state.writes[0][2] == 3

    def test_writes_go_out_at_the_lowest_priority_by_default(self):
        coordinator, entry, state, _h = build()
        entity = find(collect(climate, entry), 0)
        run(entity.async_turn_on())
        assert state.writes[0][3] == 16

    def test_the_priority_can_be_changed(self):
        coordinator, entry, state, _h = build(options={OPT_WRITE_PRIORITY: 8})
        entity = find(collect(climate, entry), 0)
        run(entity.async_turn_on())
        assert state.writes[0][3] == 8

    def test_an_unknown_mode_is_refused_rather_than_guessed(self):
        coordinator, entry, _s, _h = build()
        entity = find(collect(climate, entry), 0)
        with pytest.raises(HomeAssistantError):
            run(entity.async_set_hvac_mode(HVACMode.HEAT_COOL))

    def test_a_gateway_refusal_surfaces_as_an_error(self):
        coordinator, entry, state, _h = build()
        state.refuse_writes = True
        entity = find(collect(climate, entry), 0)
        with pytest.raises(HomeAssistantError) as caught:
            run(entity.async_turn_on())
        assert "write-access-denied" in str(caught.value)


class TestLivingWithAHeadEnd:
    def test_a_priority_held_elsewhere_stops_the_write(self):
        # Overriding the building's own control would look entirely normal
        # from the BMS while its schedule stopped working.
        state = fake.FakeGatewayState(populated={0})
        point = PointRef(0, MEMBERS_BY_KEY["on_off_command"])
        state.priorities[point.instance] = [8]
        coordinator, entry, state, _h = build(state)
        entity = find(collect(climate, entry), 0)
        with pytest.raises(HomeAssistantError) as caught:
            run(entity.async_turn_on())
        message = str(caught.value)
        assert "priority 8" in message
        assert "building management" in message
        assert state.writes == []

    def test_our_own_earlier_write_is_not_treated_as_contention(self):
        state = fake.FakeGatewayState(populated={0})
        point = PointRef(0, MEMBERS_BY_KEY["on_off_command"])
        state.priorities[point.instance] = [16]
        coordinator, entry, state, _h = build(state)
        entity = find(collect(climate, entry), 0)
        run(entity.async_turn_on())
        assert state.writes

    def test_the_priority_survey_is_reported(self):
        state = fake.FakeGatewayState(populated={0})
        point = PointRef(0, MEMBERS_BY_KEY["on_off_command"])
        state.priorities[point.instance] = [8]
        coordinator, entry, _s, _h = build(state)
        entity = find(collect(climate, entry), 0)
        with pytest.raises(HomeAssistantError):
            run(entity.async_turn_on())
        assert coordinator.write_summary["priorities_held_elsewhere"] == {
            point.key: [8]
        }


class TestCoalescing:
    def test_a_second_write_too_soon_is_held_not_refused(self):
        # Somebody nudging a setpoint twice should not see an error.
        coordinator, entry, state, hass = build(
            options={OPT_MIN_WRITE_INTERVAL: 60}
        )
        entity = find(collect(climate, entry), 0)

        async def scenario():
            await entity.async_set_temperature(temperature=21.0)
            await entity.async_set_temperature(temperature=22.0)
            await entity.async_set_temperature(temperature=23.0)
            return len(state.writes), coordinator.pending_writes

        sent, pending = run(scenario())
        assert sent == 1
        assert state.writes[0][2] == 21.0
        # Only the newest value is waiting; the middle one is dropped.
        assert list(pending.values()) == [23.0]

    def test_the_held_value_is_shown_immediately(self):
        coordinator, entry, _s, _h = build(options={OPT_MIN_WRITE_INTERVAL: 60})
        entity = find(collect(climate, entry), 0)

        async def scenario():
            await entity.async_set_temperature(temperature=21.0)
            await entity.async_set_temperature(temperature=23.0)

        run(scenario())
        # The control must not spring back under the user's finger.
        assert entity.target_temperature == 23.0

    def test_the_newest_held_value_is_what_gets_sent(self):
        # A real interval, short enough to wait for: the point is that the
        # held write does eventually go out, carrying the last value asked
        # for rather than the first or all three.
        coordinator, entry, state, _h = build()
        coordinator.guard.min_interval = 0.05
        entity = find(collect(climate, entry), 0)

        async def scenario():
            await entity.async_set_temperature(temperature=21.0)
            await entity.async_set_temperature(temperature=22.0)
            await entity.async_set_temperature(temperature=23.0)
            assert len(state.writes) == 1
            await asyncio.sleep(0.25)
            return [w[2] for w in state.writes]

        assert run(scenario()) == [21.0, 23.0]
        assert coordinator.pending_writes == {}

    def test_the_daily_cap_still_refuses_rather_than_holding(self):
        coordinator, entry, state, _h = build(
            options={OPT_MIN_WRITE_INTERVAL: 60}
        )
        coordinator.guard.daily_cap = 1
        entity = find(collect(climate, entry), 0)

        async def scenario():
            await entity.async_set_temperature(temperature=21.0)
            with pytest.raises(HomeAssistantError) as caught:
                await entity.async_set_temperature(temperature=22.0)
            return str(caught.value)

        assert "daily allowance" in run(scenario())


class TestSwitchesAndButtons:
    def test_gated_switches_stay_hidden_by_default(self):
        _c, entry, _s, _h = build()
        keys = {e.member.key for e in collect(switch, entry)}
        assert "system_forced_off" not in keys
        assert "remote_lock_on_off" not in keys
        # The harmless ones are there.
        assert "energy_saving_command" in keys

    def test_gated_switches_appear_when_asked_for(self):
        _c, entry, _s, _h = build(options={OPT_GATED: True})
        keys = {e.member.key for e in collect(switch, entry)}
        assert "system_forced_off" in keys
        assert "remote_lock_setpoint" in keys

    def test_a_switch_reads_its_status_object_not_its_command(self):
        # A command object reports what was last asked of it.
        _c, entry, _s, _h = build()
        entity = next(
            e for e in collect(switch, entry)
            if e.member.key == "energy_saving_command"
        )
        assert entity.extra_state_attributes["read_from"] == (
            "EnergyEfficiencyStatus"
        )

    def test_a_gated_switch_says_so(self):
        _c, entry, _s, _h = build(options={OPT_GATED: True})
        entity = next(
            e for e in collect(switch, entry)
            if e.member.key == "remote_lock_on_off"
        )
        assert entity.extra_state_attributes["affects_occupants"] is True

    def test_the_filter_reset_is_a_button(self):
        _c, entry, state, _h = build()
        buttons = collect(button, entry)
        assert {e.member.key for e in buttons} == {"filter_sign_reset"}
        run(find(buttons, 0).async_press())
        assert state.writes[0][2] == "active"

    def test_a_button_is_available_without_a_reading(self):
        # A reset has nothing to read, so availability cannot depend on one.
        _c, entry, _s, _h = build()
        assert find(collect(button, entry), 0).available


class TestFaultEvents:
    def test_a_new_fault_fires_once_with_its_code(self):
        state = fake.FakeGatewayState(populated={0})
        coordinator, entry, state, _h = build(state)
        entity = find(collect(event, entry), 0)
        run(entity.async_added_to_hass())

        state.faulted.add(0)
        run(coordinator.async_refresh())
        entity._handle_coordinator_update()

        assert entity.events == [("fault", {"fault_code": "U1"})]

    def test_clearing_fires_too(self):
        state = fake.FakeGatewayState(populated={0}, faulted={0})
        coordinator, entry, state, _h = build(state)
        entity = find(collect(event, entry), 0)
        run(entity.async_added_to_hass())

        state.faulted.clear()
        run(coordinator.async_refresh())
        entity._handle_coordinator_update()
        assert entity.events == [("cleared", {})]

    def test_an_already_faulted_unit_is_not_announced_at_startup(self):
        # Otherwise every restart reports every standing fault as new.
        state = fake.FakeGatewayState(populated={0}, faulted={0})
        coordinator, entry, _s, _h = build(state)
        entity = find(collect(event, entry), 0)
        run(entity.async_added_to_hass())
        entity._handle_coordinator_update()
        assert entity.events == []

    def test_a_steady_fault_does_not_fire_again(self):
        state = fake.FakeGatewayState(populated={0})
        coordinator, entry, state, _h = build(state)
        entity = find(collect(event, entry), 0)
        run(entity.async_added_to_hass())

        state.faulted.add(0)
        run(coordinator.async_refresh())
        entity._handle_coordinator_update()
        run(coordinator.async_refresh())
        entity._handle_coordinator_update()
        assert len(entity.events) == 1


class TestLettingGo:
    """A BACnet write holds its priority until it is relinquished.

    Without this, turning one unit on would leave Home Assistant holding
    that unit's power at priority 16 for good — the same trap as a head-end
    holding a point, with us as the stuck holder.
    """

    def test_a_write_is_recorded_as_held(self):
        coordinator, entry, state, _h = build()
        entity = find(collect(climate, entry), 0)
        run(entity.async_turn_on())
        point = PointRef(0, MEMBERS_BY_KEY["on_off_command"])
        assert coordinator.held_points == {point.key: 16}

    def test_releasing_writes_null_at_the_same_priority(self):
        from bacpypes3.primitivedata import Null

        coordinator, entry, state, _h = build()
        entity = find(collect(climate, entry), 0)
        run(entity.async_turn_on())
        state.writes.clear()

        released = run(coordinator.async_release())

        assert released == ["StartStopCommand_000"]
        assert len(state.writes) == 1
        _type, _instance, value, priority = state.writes[0]
        assert isinstance(value, Null)
        assert priority == 16

    def test_releasing_forgets_what_was_held(self):
        coordinator, entry, _s, _h = build()
        entity = find(collect(climate, entry), 0)
        run(entity.async_turn_on())
        run(coordinator.async_release())
        assert coordinator.held_points == {}

    def test_one_group_can_be_released_on_its_own(self):
        coordinator, entry, state, _h = build()
        entities = collect(climate, entry)
        run(find(entities, 0).async_turn_on())
        run(find(entities, 1).async_turn_off())
        state.writes.clear()

        released = run(coordinator.async_release(ac_number=1))

        assert released == ["StartStopCommand_001"]
        # Group 0 is still ours and still held.
        assert list(coordinator.held_points) == ["0_on_off_command"]

    def test_a_point_we_never_wrote_is_not_ours_to_release(self):
        # Writing Null at a priority we do not hold is a write in its own
        # right, and on a shared gateway that is somebody else's slot.
        coordinator, entry, state, _h = build()
        assert run(coordinator.async_release()) == []
        assert state.writes == []

    def test_a_refusal_to_release_is_reported_not_swallowed(self, caplog):
        coordinator, entry, state, _h = build()
        entity = find(collect(climate, entry), 0)
        run(entity.async_turn_on())
        state.refuse_writes = True
        assert run(coordinator.async_release()) == []
        assert "could not release" in caplog.text
        # Still recorded as held, because it still is.
        assert coordinator.held_points
