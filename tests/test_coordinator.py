"""Discovery and sweeping, against a synthetic gateway."""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.daikin_dbacs import gateway as gw
from custom_components.daikin_dbacs.const import (
    OPT_COV_LIFETIME,
    OPT_GROUPS,
    OPT_MEMBERS,
    OPT_SCAN_INTERVAL,
    OPT_USE_COV,
)
from custom_components.daikin_dbacs.coordinator import DbacsCoordinator
from custom_components.daikin_dbacs.objects import (
    DEFAULT_MEMBERS,
    MEMBERS,
    address_label,
    instance_of,
)

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

    def test_the_sweep_interval_has_a_sensible_default(self):
        # Two minutes: setpoint and mode are not among the subscribed points,
        # so the sweep is what makes a wall-controller change show up.
        coordinator, _ = build()
        assert coordinator.scan_interval == 120

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

    def test_a_notification_does_not_postpone_the_sweep(self):
        """Change-of-value must not restart the refresh timer.

        Home Assistant's async_set_updated_data restarts it. With
        notifications arriving every second or two, the sweep was pushed
        back for ever, so setpoint, mode and fan (which are only swept)
        froze at their first reading while room temperature kept moving.
        """
        state = fake.FakeGatewayState(populated={0})
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        run(coordinator.async_refresh())
        before = len(coordinator.updates)

        for value in (29.5, 29.0, 28.5):
            coordinator._cov_update(coordinator.points()[0], value)
        assert coordinator.refresh_timer_resets == 0
        assert len(coordinator.updates) == before + 3


class TestSubscriptionRefreshStagger:
    """Why the lifetimes differ from one subscription to the next.

    bacpypes3 schedules its own refresh `lifetime - 2` seconds after each
    subscription is made. Every subscription here is made within a second or
    two of the others at startup, so one shared lifetime means all of them
    refresh in the same instant — on this site 384 confirmed requests at once
    to a gateway with a 1024-byte APDU. That stalled the event loop about
    thirty minutes after every start, until the Supervisor watchdog gave up
    on the API and restarted Home Assistant, every thirty-eight minutes for
    most of a day.

    Giving each subscription its own lifetime spreads the refreshes and keeps
    them spread, because the periods differ too.
    """

    def _lifetimes(self, groups, **options):
        state = fake.FakeGatewayState(populated=set(range(groups)),
                                      cov_budget=10_000)
        coordinator, _ = build(state, options=options)
        run(coordinator.async_discover())
        run(coordinator.async_setup_cov())
        return state.lifetimes, coordinator

    def test_no_two_subscriptions_refresh_at_the_same_moment(self):
        lifetimes, _ = self._lifetimes(24)
        assert len(lifetimes) == 96
        assert len(set(lifetimes)) == len(lifetimes)

    def test_the_configured_lifetime_is_the_longest_one_used(self):
        """The option stays an upper bound, so nothing outlives it."""
        lifetimes, coordinator = self._lifetimes(24)
        assert max(lifetimes) == coordinator.cov_lifetime
        assert min(lifetimes) < coordinator.cov_lifetime

    def test_the_spread_is_wide_enough_to_matter(self):
        """Refreshes a few milliseconds apart would be no better."""
        lifetimes, _ = self._lifetimes(24)
        spread = max(lifetimes) - min(lifetimes)
        gap = spread / (len(lifetimes) - 1)
        assert spread >= 300
        assert gap >= 1

    def test_a_single_subscription_uses_the_configured_lifetime(self):
        lifetimes, coordinator = self._lifetimes(1, **{OPT_MEMBERS: ["room_temp"]})
        assert lifetimes == [coordinator.cov_lifetime]

    def test_no_lifetime_is_short_enough_to_be_its_own_problem(self):
        """A refresh every few seconds would be worse than the storm."""
        lifetimes, _ = self._lifetimes(24)
        assert min(lifetimes) >= 120

    def test_a_short_configured_lifetime_is_still_spread(self):
        lifetimes, _ = self._lifetimes(24, **{OPT_COV_LIFETIME: 300})
        assert len(set(lifetimes)) > 1
        assert min(lifetimes) >= 120
        assert max(lifetimes) == 300

    def test_rebuilding_after_a_power_cycle_spreads_them_again(self):
        state = fake.FakeGatewayState(populated=set(range(24)),
                                      cov_budget=10_000)
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        run(coordinator.async_setup_cov())

        state.subscriptions.clear()
        state.lifetimes.clear()
        state.database_revision += 1
        run(coordinator.async_refresh())

        assert len(state.lifetimes) == 96
        assert len(set(state.lifetimes)) == len(state.lifetimes)


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


class TestObjectListDiscovery:
    """Discovery from the gateway's own Object_List.

    Shaped after a real gateway: ninety-odd groups that publish twenty-eight
    members, a couple that publish two more, no ventilation members at all
    because the firmware predates them, and one object the integration does
    not model.
    """

    def _state(self):
        # Members 32-35 are absent on older firmware; heater status is the
        # kind of point two units carry and the rest do not. A command
        # member would not do here, because the point list does not govern
        # commands.
        common = set(MEMBERS) - {31, 32, 33, 34, 35}
        richer = set(MEMBERS) - {32, 33, 34, 35}
        populated = set(range(0, 9)) | set(range(10, 16))
        members = {ac: set(common) for ac in populated}
        members[0] = set(richer)
        members[1] = set(richer)
        return fake.FakeGatewayState(
            populated=populated,
            members=members,
            # A member number nothing models, as a firmware revision may add.
            extra_objects=[("binary-input", 99)],
        )

    def test_finds_the_groups_the_list_names(self):
        coordinator, _ = build(self._state())
        discovery = run(coordinator.async_discover())
        # The gap at 1-09 is a real shape: a line with one address unwired.
        assert len(discovery.live_numbers) == 15
        assert 9 not in discovery.live_numbers
        assert discovery.discovered_by == "its Object_List"

    def test_records_which_members_each_group_publishes(self):
        coordinator, _ = build(self._state())
        discovery = run(coordinator.async_discover())
        assert 31 in discovery.members_of(0)
        assert 31 not in discovery.members_of(5)
        # Firmware decides whether the ventilation members exist at all.
        assert 32 not in discovery.members_of(0)

    def test_reads_no_point_the_gateway_does_not_have(self):
        coordinator, _ = build(
            self._state(),
            options={OPT_MEMBERS: ["room_temp", "heater_status"]},
        )
        run(coordinator.async_discover())
        points = coordinator.points()
        heater = {p.ac_number for p in points if p.member.key == "heater_status"}
        # Only the two groups that carry it, not all fifteen.
        assert heater == {0, 1}
        assert len([p for p in points if p.member.key == "room_temp"]) == 15

    def test_a_sweep_of_a_narrowed_list_errors_on_nothing(self):
        coordinator, _ = build(
            self._state(),
            options={OPT_MEMBERS: ["room_temp", "heater_status"]},
        )
        run(coordinator.async_discover())
        data = run(coordinator._async_update_data())
        assert data
        assert all(reading.ok for reading in data.values())

    def test_an_object_it_does_not_model_is_recorded_not_guessed(self):
        coordinator, _ = build(self._state())
        discovery = run(coordinator.async_discover())
        assert discovery.unrecognised == {"binary-input member 99": 1}

    def test_an_object_whose_type_is_wrong_is_not_decoded(self):
        # The object type is the top ten bits of the identifier, so a type
        # that does not match the member is not the object we would take it
        # for.
        state = self._state()
        state.extra_objects = [("analog-input", instance_of(0, 2))]
        coordinator, _ = build(state)
        discovery = run(coordinator.async_discover())
        assert any("analog-input" in kind for kind in discovery.unrecognised)

    def test_falls_back_to_probing_when_the_list_is_refused(self):
        state = self._state()
        state.serves_object_list = False
        coordinator, _ = build(state)
        discovery = run(coordinator.async_discover())
        assert discovery.discovered_by == "probing every slot"
        assert len(discovery.live_numbers) == 15
        # Probing cannot tell which members a group carries, so the full set
        # is assumed rather than invented.
        assert discovery.members_of(0) is None

    def test_a_probed_group_still_gets_its_points(self):
        state = self._state()
        state.serves_object_list = False
        coordinator, _ = build(state, options={OPT_MEMBERS: ["room_temp"]})
        run(coordinator.async_discover())
        assert len(coordinator.points()) == 15

    def test_change_of_value_skips_a_member_a_group_lacks(self):
        state = self._state()
        for ac in state.members:
            state.members[ac].discard(3)  # no alarm object anywhere
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        run(coordinator.async_setup_cov())
        subscribed = {
            member for _type, member in
            [(t, i % 256) for t, i in state.subscriptions]
        }
        assert 3 not in subscribed
