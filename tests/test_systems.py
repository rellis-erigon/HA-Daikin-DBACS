"""Refrigerant systems, rolled up from the units on them.

Shaped after the fault this was built for: one condenser down, three of its
eight indoor units raising a code because they happened to be calling, and
five reading Normal while being just as badly served.
"""

from __future__ import annotations

import pytest

from custom_components.daikin_dbacs import systems
from custom_components.daikin_dbacs.gateway import PointValue
from custom_components.daikin_dbacs.objects import MEMBERS_BY_KEY, PointRef


def reading(ac, key, value):
    return PointValue(PointRef(ac, MEMBERS_BY_KEY[key]), value=value)


def board(units, mode=2):
    """Readings for a set of units: {ac: (powered, calling, comp, alarm)}.

    `mode` is Daikin's air-conditioning mode: 2 is heat, 3 is fan-only and
    is the one that does not need the compressor.
    """
    out = {}
    for ac, (powered, calling, comp, alarm) in units.items():
        out[f"{ac}_on_off_status"] = reading(ac, "on_off_status", int(powered))
        out[f"{ac}_thermo_status"] = reading(ac, "thermo_status", int(calling))
        out[f"{ac}_compressor_status"] = reading(ac, "compressor_status", int(comp))
        out[f"{ac}_alarm"] = reading(ac, "alarm", int(alarm))
        out[f"{ac}_mode_status"] = reading(ac, "mode_status", mode)
    return out


SYSTEM = systems.System(name="S-01", outdoor="CU-S-01",
                        ac_numbers=tuple(range(8)))


class TestGrouping:
    def test_units_group_by_their_system(self):
        class Entry:
            def __init__(self, system, outdoor):
                self.system = system
                self.outdoor = outdoor

        mapping = {
            0: Entry("S-01", "CU-S-01"),
            1: Entry("S-01", "CU-S-01"),
            2: Entry("S-02", "CU-S-02"),
        }
        found = systems.systems_from_mapping(mapping)
        assert set(found) == {"S-01", "S-02"}
        assert found["S-01"].ac_numbers == (0, 1)
        assert found["S-01"].outdoor == "CU-S-01"

    def test_a_unit_with_no_system_is_left_out(self):
        class Entry:
            system = None
            outdoor = None

        assert systems.systems_from_mapping({0: Entry()}) == {}

    def test_the_label_prefers_the_outdoor_unit(self):
        assert SYSTEM.label == "CU-S-01"
        assert systems.System("S-9", None, ()).label == "S-9"


class TestHealthySystem:
    def _rollup(self):
        units = {ac: (True, True, True, False) for ac in range(8)}
        return systems.roll_up(SYSTEM, board(units), set(range(8)))

    def test_the_compressor_reads_running(self):
        assert self._rollup().compressor_running is True

    def test_nothing_is_faulted(self):
        assert self._rollup().faulted is False

    def test_it_is_delivering(self):
        assert self._rollup().not_delivering is False

    def test_the_counts_add_up(self):
        r = self._rollup()
        assert (r.indoor_total, r.answering, r.running, r.calling) == (8, 8, 8, 8)

    def test_the_code_reads_normal_by_absence(self):
        assert self._rollup().fault_code is None


class TestAnOutdoorUnitThatHasStopped:
    """The fault as it actually presented."""

    def _rollup(self):
        # As the real fault read: units switched on in heat, the compressor
        # off, three of them raising a code — and the thermostat flag off on
        # every one of them, because a unit that cannot reach its setpoint
        # stops calling.
        units = {}
        for ac in range(8):
            on = ac in (2, 4, 6)
            units[ac] = (on, False, False, on)
        codes = {2: "E5", 4: "E5", 6: "E5"}
        return systems.roll_up(SYSTEM, board(units), set(range(8)), codes)

    def test_the_compressor_reads_stopped(self):
        assert self._rollup().compressor_running is False

    def test_three_units_are_in_alarm_not_eight(self):
        # The five that are not running cannot see the fault.
        r = self._rollup()
        assert r.in_alarm == 3
        assert r.running == 3

    def test_nothing_calling_is_not_a_fault_however_it_looks(self):
        # Measured across fourteen live systems: the compressor follows
        # demand, so stopped-with-nothing-calling is how a satisfied system
        # is supposed to read. An earlier version of this fired on six of
        # the fourteen.
        r = self._rollup()
        assert r.calling == 0
        assert r.not_delivering is False

    def test_calling_with_no_compressor_is_the_fault(self):
        # The entity this module exists for: units asking, nothing coming.
        units = {ac: (True, ac in (2, 4, 6), False, ac in (2, 4, 6))
                 for ac in range(8)}
        r = systems.roll_up(SYSTEM, board(units), set(range(8)),
                            {2: "E5", 4: "E5", 6: "E5"})
        assert r.calling == 3
        assert r.compressor_running is False
        assert r.not_delivering is True

    def test_the_code_is_reported_once_because_they_agree(self):
        assert self._rollup().fault_code == "E5"

    def test_the_units_in_alarm_are_named(self):
        assert self._rollup().alarm_addresses == [
            "1 · 1-02", "1 · 1-04", "1 · 1-06"
        ]

    def test_disagreeing_codes_are_all_reported(self):
        units = {ac: (True, True, False, ac in (0, 1)) for ac in range(8)}
        r = systems.roll_up(SYSTEM, board(units), set(range(8)),
                            {0: "E5", 1: "U1"})
        assert r.fault_code == "E5, U1"


class TestAnIdleSystem:
    def test_a_stopped_compressor_with_nothing_running_is_not_a_fault(self):
        # A system nobody is asking anything of is behaving correctly.
        units = {ac: (False, False, False, False) for ac in range(8)}
        r = systems.roll_up(SYSTEM, board(units), set(range(8)))
        assert r.compressor_running is False
        assert r.not_delivering is False
        assert r.faulted is False

    def test_units_on_but_satisfied_are_not_a_fault(self):
        # Eight units switched on, none calling: exactly what a system that
        # has reached its setpoints looks like.
        units = {ac: (True, False, False, False) for ac in range(8)}
        r = systems.roll_up(SYSTEM, board(units), set(range(8)))
        assert r.running == 8
        assert r.calling == 0
        assert r.not_delivering is False


class TestWhatItRefusesToSay:
    def test_a_system_with_nothing_answering_says_nothing(self):
        r = systems.roll_up(SYSTEM, {}, set(range(8)))
        assert r.compressor_running is None
        assert r.faulted is None
        assert r.not_delivering is None

    def test_units_the_gateway_does_not_have_are_not_counted(self):
        # A decommissioned unit left in the mapping file would otherwise
        # make a healthy system look half dead.
        units = {ac: (True, True, True, False) for ac in range(3)}
        r = systems.roll_up(SYSTEM, board(units), {0, 1, 2})
        assert r.indoor_total == 3
        assert r.answering == 3

    def test_a_point_that_errored_does_not_count_as_a_reading(self):
        data = board({0: (True, True, True, False)})
        data["0_on_off_status"] = PointValue(
            PointRef(0, MEMBERS_BY_KEY["on_off_status"]), error="unknown-object"
        )
        r = systems.roll_up(SYSTEM, data, {0})
        assert r.answering == 0
        assert r.compressor_running is None


class TestTheReport:
    def test_the_summary_carries_what_an_engineer_needs(self):
        units = {ac: (True, True, False, ac == 0) for ac in range(4)}
        r = systems.roll_up(SYSTEM, board(units), set(range(4)), {0: "E5"})
        d = r.as_dict()
        assert d["outdoor_unit"] == "CU-S-01"
        assert d["compressor_running"] is False
        assert d["not_delivering"] is True
        assert d["fault_code"] == "E5"
        assert d["units_in_alarm"] == ["1 · 1-00"]
