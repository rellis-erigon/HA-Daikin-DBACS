"""A change is reported sent, then confirmed or not, from the unit's status."""

from __future__ import annotations

import pytest

from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import HomeAssistantError

from custom_components.daikin_dbacs import climate
from custom_components.daikin_dbacs.confirm import matches
from custom_components.daikin_dbacs.objects import MEMBERS_BY_KEY, instance_of

from test_control import build, collect, find, run


def unit_one():
    coordinator, entry, state, hass = build()
    return coordinator, state, hass, find(collect(climate, entry, hass), 1)


def reports(state, ac_number, key, value):
    state.overrides[instance_of(ac_number, MEMBERS_BY_KEY[key].number)] = value


def sweep(coordinator, hass, seconds=1.0):
    hass.loop.advance(seconds)
    run(coordinator.async_refresh())


class TestConfirmed:
    def test_a_mode_change_waits_then_confirms(self):
        coordinator, state, hass, unit = unit_one()   # unit 1 is on, in Heat
        run(unit.async_set_hvac_mode(HVACMode.COOL))
        pending = unit.extra_state_attributes["pending_changes"]
        assert pending == [{"setting": "hvac_mode", "value": "cool", "state": "sent"}]
        assert "last_change" not in unit.extra_state_attributes

        reports(state, 1, "mode_status", 1)
        sweep(coordinator, hass)
        attributes = unit.extra_state_attributes
        assert "pending_changes" not in attributes
        assert attributes["last_change"]["setting"] == "hvac_mode"
        assert attributes["last_change"]["result"] == "confirmed"
        assert attributes["last_change"]["reported"] == "cool"

    def test_a_setpoint_is_not_confirmed_by_our_own_echo(self):
        coordinator, state, hass, unit = unit_one()   # unit 1 reads 22.0
        run(unit.async_set_temperature(temperature=23.0))
        assert unit.target_temperature == 23.0          # shown at once
        sweep(coordinator, hass)                        # unit still says 22
        assert unit.extra_state_attributes["pending_changes"][0]["setting"] == "temperature"

        reports(state, 1, "setpoint", 23.0)
        sweep(coordinator, hass)
        assert unit.extra_state_attributes["last_change"]["result"] == "confirmed"


class TestNotConfirmed:
    def test_a_unit_that_never_follows_is_reported(self):
        coordinator, state, hass, unit = unit_one()   # unit 1's fan is high
        run(unit.async_set_fan_mode("low"))
        sweep(coordinator, hass, coordinator.confirm_within + 1)
        last = unit.extra_state_attributes["last_change"]
        assert last["result"] == "not_confirmed"
        assert last["value"] == "low"
        assert last["reported"] == "high"
        assert "pending_changes" not in unit.extra_state_attributes

    def test_a_refused_write_says_why(self):
        coordinator, state, hass, unit = unit_one()
        state.refuse_writes = True
        with pytest.raises(HomeAssistantError):
            run(unit.async_set_fan_mode("low"))
        last = unit.extra_state_attributes["last_change"]
        assert last["result"] == "refused"
        assert last["reason"]


def test_auto_is_confirmed_by_the_mode_it_chose():
    assert matches("mode_command", 4, 1)
    assert matches("mode_command", 4, 2)
    assert not matches("mode_command", 4, 3)
    assert matches("on_off_command", "active", 1)
    assert not matches("on_off_command", "inactive", 1)
    assert matches("setpoint", 22.5, 22.5)
    assert not matches("setpoint", 22.5, 22.0)


def test_the_unit_says_where_it_lives_on_the_gateway():
    """Enough on the climate entity to check its mapping by hand."""
    _coordinator, _state, _hass, unit = unit_one()
    attributes = unit.extra_state_attributes
    assert attributes["ac_number"] == 1
    assert attributes["gateway_host"] == "192.0.2.10"
    assert attributes["gateway_device_instance"] is not None
    objects = {o["object"]: o for o in attributes["bacnet_objects"]}
    assert objects["AirConModeCommand_001"] == {
        "point": MEMBERS_BY_KEY["mode_command"].label,
        "object": "AirConModeCommand_001",
        "type": "multi-state-output",
        "instance": 256 + 5,
        "kind": "command",
    }
    assert objects["StartStopStatus_001"]["kind"] == "status"
    # Fixed facts stay out of the recorder.
    assert "bacnet_objects" in unit._unrecorded_attributes


def test_a_change_is_read_back_without_waiting_for_a_sweep():
    """The status is read on its own soon after the write, so a slow sweep
    cannot run the confirmation out of time."""
    import asyncio

    coordinator, state, hass, unit = unit_one()
    coordinator.CHECK_AFTER = (0,)
    reports(state, 1, "setpoint", 23.0)        # what the unit will say

    async def go():
        await unit.async_set_temperature(temperature=23.0)
        await asyncio.sleep(0.05)              # let the read-back run

    run(go())
    attributes = unit.extra_state_attributes
    assert "pending_changes" not in attributes
    assert attributes["last_change"]["result"] == "confirmed"
