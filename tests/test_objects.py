"""The object model: the instance formula, the addresses, the labels."""

from __future__ import annotations

import pytest

from custom_components.daikin_dbacs import objects as o


class TestInstanceNumbers:
    """Checked against the gateway's own published instance table.

    These four are the corners of Daikin's 256-row table. If the formula
    drifts, every point in the integration silently addresses the wrong
    indoor unit, so these are the most load-bearing assertions in the suite.
    """

    @pytest.mark.parametrize(
        ("port", "line", "group", "member", "instance"),
        [
            (1, 1, 0, 1, 1),  # first group, first member
            (1, 2, 0, 1, 4097),  # second line of port 1
            (2, 1, 0, 1, 16385),  # first group of port 2
            (4, 4, 15, 1, 65281),  # last group of the last port
            (1, 1, 0, 31, 31),  # last documented member of the first group
            (3, 1, 0, 1, 32769),  # first group of port 3
        ],
    )
    def test_matches_the_published_table(self, port, line, group, member, instance):
        ac_number = o.ac_number_of(port, line, group)
        assert o.instance_of(ac_number, member) == instance

    def test_round_trips_through_every_slot(self):
        for ac_number in range(o.MAX_AC_NUMBER + 1):
            port, line, group = o.diii_address(ac_number)
            assert o.ac_number_of(port, line, group) == ac_number
            for member in (1, 19, 35):
                instance = o.instance_of(ac_number, member)
                assert o.split_instance(instance) == (ac_number, member)

    def test_the_table_is_256_distinct_groups(self):
        labels = {o.address_label(ac) for ac in range(o.MAX_AC_NUMBER + 1)}
        assert len(labels) == 256

    @pytest.mark.parametrize(
        ("ac_number", "label"),
        [(0, "1 · 1-00"), (16, "1 · 2-00"), (64, "2 · 1-00"),
         (255, "4 · 4-15")],
    )
    def test_labels_read_the_way_daikin_writes_them(self, ac_number, label):
        assert o.address_label(ac_number) == label

    @pytest.mark.parametrize("ac_number", [-1, 256, 1000])
    def test_refuses_an_impossible_group(self, ac_number):
        with pytest.raises(ValueError):
            o.instance_of(ac_number, 1)

    @pytest.mark.parametrize(
        ("port", "line", "group"), [(0, 1, 0), (5, 1, 0), (1, 0, 0), (1, 5, 0), (1, 1, 16)]
    )
    def test_refuses_an_impossible_address(self, port, line, group):
        with pytest.raises(ValueError):
            o.ac_number_of(port, line, group)


class TestMemberTable:
    def test_vacant_members_are_absent_and_documented(self):
        assert 15 not in o.MEMBERS
        assert set(o.VACANT) == {15}

    def test_member_18_is_modelled_despite_being_called_vacant(self):
        # The specification names no object for member 18 and marks it
        # Japanese market only, but real gateways publish an Accumulator
        # there. Leaving it out made 96 identical "unrecognised object"
        # entries on a live system.
        assert o.MEMBERS[18].object_type is o.ObjType.ACCUMULATOR
        assert not o.MEMBERS[18].default_on
        assert "Japanese market" in o.MEMBERS[18].note

    def test_covers_every_documented_member(self):
        expected = set(range(1, 36)) - set(o.VACANT)
        assert set(o.MEMBERS) == expected

    def test_object_types_match_the_specification(self):
        # A wrong object type addresses a different object entirely, because
        # the type is the top ten bits of the identifier.
        expected = {
            1: o.ObjType.BO,
            2: o.ObjType.BI,
            3: o.ObjType.BI,
            4: o.ObjType.MI,
            5: o.ObjType.MO,
            6: o.ObjType.MI,
            9: o.ObjType.AI,
            10: o.ObjType.AV,
            19: o.ObjType.ACCUMULATOR,
            22: o.ObjType.AV,
            23: o.ObjType.AI,
        }
        for number, object_type in expected.items():
            assert o.MEMBERS[number].object_type is object_type

    def test_keys_are_unique(self):
        keys = [member.key for member in o.MEMBERS.values()]
        assert len(keys) == len(set(keys))

    def test_every_status_point_is_on_by_default(self):
        # The more points the better, so everything a group reports is
        # enabled. Commands and resets are not readings, and the two
        # accumulators are model-dependent and read zero on units that do
        # not support them, which looks like a broken meter rather than an
        # absent one.
        expected = {
            member.key
            for member in o.READABLE
            if member.kind is o.Kind.STATUS
            and member.unit not in ("energy", "volume")
        }
        assert set(o.DEFAULT_MEMBERS) == expected
        assert len(o.DEFAULT_MEMBERS) == 18

    def test_the_accumulators_stay_off(self):
        assert not o.MEMBERS_BY_KEY["accumulated_power"].default_on
        assert not o.MEMBERS_BY_KEY["accumulated_gas"].default_on

    def test_change_of_value_is_the_four_decided_on(self):
        assert set(o.COV_MEMBERS) == {
            "on_off_status",
            "alarm",
            "malfunction_code",
            "room_temp",
        }

    def test_every_cov_member_is_also_on_by_default(self):
        # A subscription to a point no entity carries would be a slot spent
        # on the gateway for nothing.
        for key in o.COV_MEMBERS:
            assert key in o.DEFAULT_MEMBERS

    def test_the_probe_member_is_published_for_every_group(self):
        assert o.MEMBERS[o.PROBE_MEMBER].key == "on_off_status"

    def test_gated_members_are_the_ones_that_affect_people(self):
        gated = {m.key for m in o.MEMBERS.values() if m.gated}
        assert gated == {
            "remote_lock_on_off",
            "remote_lock_mode",
            "remote_lock_setpoint",
            "central_control_rejection",
            "system_forced_off",
            "forced_thermo_off_command",
        }


class TestStateLabels:
    def test_airflow_is_low_high_middle_auto(self):
        # Daikin's enumeration really is in that order. Reading it as
        # low-medium-high mislabels the fan speed on every unit.
        member = o.MEMBERS[8]
        assert [o.member_state(member, v) for v in (1, 2, 3, 4)] == [
            "Low",
            "High",
            "Middle",
            "Auto",
        ]

    def test_mode_status_has_a_documented_hole(self):
        member = o.MEMBERS[6]
        assert o.member_state(member, 1) == "Cool"
        assert o.member_state(member, 3) == "Fan"
        # State 4 is documented as unused, so it is reported as the raw value
        # rather than labelled with a guess.
        assert o.member_state(member, 4) == "4"
        assert o.member_state(member, 5) == "Dry"

    def test_mode_command_does_use_four(self):
        assert o.member_state(o.MEMBERS[5], 4) == "Auto"

    def test_communication_status_is_active_when_faulted(self):
        member = o.MEMBERS[20]
        assert o.member_state(member, 0) == "Normal"
        assert o.member_state(member, 1) == "Fault"
        assert o.member_state(member, "active") == "Fault"
        assert o.member_state(member, "inactive") == "Normal"

    def test_air_direction_is_not_contiguous(self):
        member = o.MEMBERS[23]
        assert o.member_state(member, 0) == "Horizontal"
        assert o.member_state(member, 3) == "Horizontal"
        assert o.member_state(member, 4) == "Vertical"
        assert o.member_state(member, 7) == "Swing"
        assert o.member_state(member, 5) == "5"

    def test_an_out_of_range_value_is_reported_not_guessed(self):
        assert o.member_state(o.MEMBERS[8], 9) == "9"

    def test_no_value_stays_no_value(self):
        assert o.member_state(o.MEMBERS[8], None) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, False),
            (1, True),
            ("active", True),
            ("inactive", False),
            ("ACTIVE", True),
            (None, None),
            ("nonsense", None),
        ],
    )
    def test_binary_values_arrive_in_several_spellings(self, value, expected):
        assert o.is_binary_active(value) is expected


class TestPointRefs:
    def test_names_the_object_the_way_the_gateway_does(self):
        point = o.PointRef(5, o.MEMBERS[9])
        # Three digits, because that is how the gateway spells it, and the
        # name is published as an attribute for cross-referencing.
        assert point.object_name == "RoomTemp_005"
        # bacpypes3 rejects a plain tuple, so the identifier is written
        # the way it parses.
        assert point.object_id == "analog-input,1289"
        assert point.instance == 5 * 256 + 9

    def test_points_for_limits_to_the_members_asked_for(self):
        points = o.points_for(3, ["room_temp", "setpoint", "not_a_member"])
        assert {p.member.key for p in points} == {"room_temp", "setpoint"}

    def test_points_for_defaults_to_everything_readable(self):
        points = o.points_for(3)
        assert len(points) == len(o.READABLE)
        assert all(p.member.kind is not o.Kind.RESET for p in points)

    def test_keys_are_unique_across_a_whole_gateway(self):
        keys = {
            point.key
            for ac in range(o.MAX_AC_NUMBER + 1)
            for point in o.points_for(ac)
        }
        assert len(keys) == 256 * len(o.READABLE)
