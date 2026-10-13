"""Discovery and sweeping, against a synthetic gateway."""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.daikin_dbacs import gateway as gw
from custom_components.daikin_dbacs.const import (
    OPT_GROUPS,
    OPT_MEMBERS,
    OPT_SCAN_INTERVAL,
    OPT_USE_COV,
)
from custom_components.daikin_dbacs.coordinator import DbacsCoordinator
from custom_components.daikin_dbacs.objects import DEFAULT_MEMBERS, address_label

import fake_gateway as fake


def run(coro):
    return asyncio.run(coro)


def build(state=None, options=None):
    """A coordinator wired to a synthetic gateway."""
    state = state or fake.FakeGatewayState()
    client = gw.DbacsGateway("192.0.2.10")
    fake.attach(client, state)
    info = run(client.read_info())
    entry = ConfigEntry(data={"host": "192.0.2.10"}, options=options or {})
    coordinator = DbacsCoordinator(HomeAssistant(), entry, client, info)
    return coordinator, state


class TestDiscovery:
    def test_three_outcomes_are_kept_apart(self):
        # Absent, dead and live are different things, and conflating them is
        # how a dashboard ends up confidently wrong.
        state = fake.FakeGatewayState(
            populated={0, 1, 2, 3}, dead={2}
        )
        coordinator, _ = build(state)
        discovery = run(coordinator.async_discover())

        assert discovery.live_numbers == (0, 1, 3)
        assert discovery.dead == (2,)
        assert len(discovery.absent) == 252
        assert len(discovery.groups) == 256

    def test_a_dead_group_gets_no_device_but_is_recorded(self):
        state = fake.FakeGatewayState(populated={0, 1}, dead={1})
        coordinator, _ = build(state)
        discovery = run(coordinator.async_discover())
        assert 1 not in discovery.live_numbers
        assert address_label(1) in [address_label(ac) for ac in discovery.dead]

    def test_a_group_that_comes_back_is_picked_up(self):
        state = fake.FakeGatewayState(populated={0, 1}, dead={1})
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        assert coordinator.active_groups == (0,)

        state.dead.clear()
        run(coordinator.async_discover())
        assert coordinator.active_groups == (0, 1)

    def test_a_fully_populated_gateway_is_handled(self):
        state = fake.FakeGatewayState(populated=set(range(256)))
        coordinator, _ = build(state)
        discovery = run(coordinator.async_discover())
        assert len(discovery.live_numbers) == 256
        assert discovery.absent == ()

    def test_waits_out_a_gateway_still_scanning(self, monkeypatch):
        monkeypatch.setattr(gw.asyncio, "sleep", _no_sleep)
        state = fake.FakeGatewayState(populated={0, 1})
        coordinator, _ = build(state)
        # The gateway starts rescanning its DIII-NET ports. The right answer
        # is "not yet", not "nothing there".
        state.scanning_for = 2
        discovery = run(coordinator.async_discover())
        assert discovery.live_numbers == (0, 1)

    def test_records_the_object_count_for_diagnostics(self):
        coordinator, _ = build()
        discovery = run(coordinator.async_discover())
        assert discovery.object_count and discovery.object_count > 0


class TestWhichPointsAreRead:
    def test_defaults_to_the_complete_looking_unit(self):
        coordinator, _ = build(fake.FakeGatewayState(populated={0}))
        run(coordinator.async_discover())
        assert set(coordinator.enabled_member_keys) == set(DEFAULT_MEMBERS)
        assert len(coordinator.points()) == len(DEFAULT_MEMBERS)

    def test_members_are_chosen_once_and_applied_to_every_group(self):
        state = fake.FakeGatewayState(populated={0, 1, 2})
        coordinator, _ = build(
            state, options={OPT_MEMBERS: ["room_temp", "setpoint"]}
        )
        run(coordinator.async_discover())
        points = coordinator.points()
        assert len(points) == 6
        assert {p.member.key for p in points} == {"room_temp", "setpoint"}

    def test_groups_can_be_narrowed_wholesale(self):
        state = fake.FakeGatewayState(populated={0, 1, 2, 3})
        coordinator, _ = build(
            state, options={OPT_MEMBERS: ["room_temp"], OPT_GROUPS: [0, 2]}
        )
        run(coordinator.async_discover())
        assert coordinator.active_groups == (0, 2)
        assert {p.ac_number for p in coordinator.points()} == {0, 2}

    def test_a_narrowed_group_that_is_dead_still_does_not_appear(self):
        state = fake.FakeGatewayState(populated={0, 1}, dead={1})
        coordinator, _ = build(state, options={OPT_GROUPS: [0, 1]})
        run(coordinator.async_discover())
        assert coordinator.active_groups == (0,)

    def test_an_unknown_member_key_is_ignored_not_fatal(self):
        coordinator, _ = build(
            fake.FakeGatewayState(populated={0}),
            options={OPT_MEMBERS: ["room_temp", "was_renamed_once"]},
        )
        run(coordinator.async_discover())
        assert coordinator.enabled_member_keys == ("room_temp",)

    def test_disabled_points_are_never_read(self):
        state = fake.FakeGatewayState(populated=set(range(256)))
        coordinator, _ = build(state, options={OPT_MEMBERS: ["room_temp"]})
        run(coordinator.async_discover())
        # The ceiling this keeps us under is 256 groups x 33 readable points.
        assert len(coordinator.points()) == 256

    def test_the_sweep_interval_is_the_slow_one_by_default(self):
        coordinator, _ = build()
        assert coordinator.scan_interval == 300

    def test_the_sweep_interval_can_be_set(self):
        coordinator, _ = build(options={OPT_SCAN_INTERVAL: 90})
        assert coordinator.scan_interval == 90


class TestSweeping:
    def test_a_sweep_returns_every_enabled_point(self):
        state = fake.FakeGatewayState(populated={0, 1})
        coordinator, _ = build(state, options={OPT_MEMBERS: ["room_temp"]})
        run(coordinator.async_discover())
        data = run(coordinator._async_update_data())
        assert set(data) == {"0_room_temp", "1_room_temp"}
        assert all(reading.ok for reading in data.values())

    def test_a_sweep_keeps_what_change_of_value_already_told_us(self):
        state = fake.FakeGatewayState(populated={0})
        coordinator, _ = build(
            state, options={OPT_MEMBERS: ["room_temp", "setpoint"]}
        )
        run(coordinator.async_discover())
        run(coordinator.async_refresh())

        # A point the sweep no longer asks about must not vanish, and a
        # fresher notification must not be overwritten by an older read.
        coordinator.data["0_extra"] = gw.PointValue(
            coordinator.points()[0], value=99
        )
        data = run(coordinator._async_update_data())
        assert data["0_extra"].value == 99

    def test_a_gateway_still_scanning_fails_the_sweep_clearly(self):
        state = fake.FakeGatewayState(populated={0})
        coordinator, _ = build(state, options={OPT_MEMBERS: ["room_temp"]})
        run(coordinator.async_discover())
        state.scanning_for = 99
        with pytest.raises(UpdateFailed) as caught:
            run(coordinator._async_update_data())
        assert "scanning" in str(caught.value)

    def test_communication_status_folds_back_into_the_group(self):
        state = fake.FakeGatewayState(populated={0, 1})
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        assert coordinator.active_groups == (0, 1)

        state.dead.add(1)
        run(coordinator.async_refresh())
        # A unit that stops communicating mid-session stops being live.
        assert coordinator.discovery.groups[1].communicating is False
        assert coordinator.active_groups == (0,)

    def test_an_empty_gateway_sweeps_to_nothing_without_failing(self):
        state = fake.FakeGatewayState(populated=set())
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        assert run(coordinator._async_update_data()) == {}


class TestFaultCodes:
    def test_the_two_character_code_is_read_for_a_faulted_unit(self):
        state = fake.FakeGatewayState(populated={0, 1}, faulted={1})
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        run(coordinator.async_refresh())
        assert coordinator.fault_code(1) == "U1"

    def test_a_healthy_unit_costs_no_extra_read(self):
        state = fake.FakeGatewayState(populated={0})
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        run(coordinator.async_refresh())
        assert coordinator.fault_code(0) is None

    def test_a_cleared_fault_clears_the_code(self):
        state = fake.FakeGatewayState(populated={0}, faulted={0})
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        run(coordinator.async_refresh())
        assert coordinator.fault_code(0) == "U1"

        state.faulted.clear()
        run(coordinator.async_refresh())
        assert coordinator.fault_code(0) is None


class TestChangeOfValue:
    def test_subscribes_only_the_four_decided_on(self):
        state = fake.FakeGatewayState(populated={0, 1})
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        run(coordinator.async_setup_cov())
        # Four points per group, not everything: the gateway allows five
        # subscribers per object and is shared with a head-end.
        assert coordinator.cov_summary["subscribed"] == 8

    def test_can_be_turned_off(self):
        state = fake.FakeGatewayState(populated={0})
        coordinator, _ = build(state, options={OPT_USE_COV: False})
        run(coordinator.async_discover())
        run(coordinator.async_setup_cov())
        assert coordinator.cov_summary["subscribed"] == 0

    def test_a_refusal_falls_back_to_the_sweep(self):
        state = fake.FakeGatewayState(populated={0, 1}, cov_budget=3)
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        run(coordinator.async_setup_cov())
        summary = coordinator.cov_summary
        assert summary["subscribed"] == 3
        assert summary["refused"] == 5
        # The refused points still arrive, because the sweep reads them all.
        data = run(coordinator._async_update_data())
        assert data["1_room_temp"].ok

    def test_does_not_subscribe_a_point_no_entity_carries(self):
        state = fake.FakeGatewayState(populated={0})
        coordinator, _ = build(state, options={OPT_MEMBERS: ["setpoint"]})
        run(coordinator.async_discover())
        run(coordinator.async_setup_cov())
        assert coordinator.cov_summary["subscribed"] == 0

    def test_a_notification_updates_the_data_immediately(self):
        state = fake.FakeGatewayState(populated={0})
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        run(coordinator.async_refresh())

        point = coordinator.points()[0]
        coordinator._cov_update(point, 29.5)
        assert coordinator.data[point.key].value == 29.5
        assert coordinator.updates


class TestGatewayRestart:
    def test_subscriptions_are_rebuilt_after_a_power_cycle(self):
        # The gateway drops every subscription when it loses power and says
        # nothing about it, so the only way to notice is to watch something
        # that changes across a restart.
        state = fake.FakeGatewayState(populated={0})
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        run(coordinator.async_setup_cov())
        assert coordinator.cov_summary["subscribed"] == 4

        state.subscriptions.clear()
        state.database_revision += 1
        run(coordinator.async_refresh())

        assert state.subscriptions
        assert coordinator.cov_summary["subscribed"] == 4

    def test_a_steady_gateway_is_not_resubscribed_every_sweep(self):
        state = fake.FakeGatewayState(populated={0})
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        run(coordinator.async_setup_cov())
        before = list(state.subscriptions)
        run(coordinator.async_refresh())
        assert state.subscriptions == before


async def _no_sleep(_seconds):
    return None
