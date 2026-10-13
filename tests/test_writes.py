"""The write-rate guard.

Daikin caps setting changes at 7,000 a year per point. The guard is the
reason an automation with a bad trigger cannot spend that in an afternoon,
so it is tested on its own, with the clock passed in rather than waited on.
"""

from __future__ import annotations

import pytest

from custom_components.daikin_dbacs.writes import (
    CHANGES_PER_DAY,
    CHANGES_PER_YEAR,
    WriteGuard,
    WriteRateExceeded,
    WriteRefused,
)


class TestTheDocumentedLimit:
    def test_the_annual_cap_is_daikins_number(self):
        assert CHANGES_PER_YEAR == 7000

    def test_the_daily_allowance_is_derived_from_it(self):
        assert CHANGES_PER_DAY == 19


class TestSpacing:
    def test_a_first_write_is_allowed(self):
        guard = WriteGuard()
        guard.check("0_setpoint", 1000.0)

    def test_a_second_write_too_soon_is_refused(self):
        guard = WriteGuard(min_interval=60)
        guard.record("0_setpoint", 1000.0)
        with pytest.raises(WriteRefused) as caught:
            guard.check("0_setpoint", 1010.0)
        # The message has to say when, or the only recourse is guessing.
        assert "51s" in str(caught.value)

    def test_a_second_write_after_the_interval_is_allowed(self):
        guard = WriteGuard(min_interval=60)
        guard.record("0_setpoint", 1000.0)
        guard.check("0_setpoint", 1061.0)

    def test_spacing_is_per_point_not_per_gateway(self):
        guard = WriteGuard(min_interval=60)
        guard.record("0_setpoint", 1000.0)
        # A different unit is a different point and must not be held up.
        guard.check("1_setpoint", 1000.0)


class TestTheDailyCap:
    def test_the_allowance_runs_out(self):
        guard = WriteGuard(min_interval=0, daily_cap=3)
        for tick in range(3):
            guard.check("0_setpoint", 1000.0 + tick)
            guard.record("0_setpoint", 1000.0 + tick)
        with pytest.raises(WriteRateExceeded) as caught:
            guard.check("0_setpoint", 1003.0)
        assert "daily allowance" in str(caught.value)
        assert "7000" in str(caught.value)

    def test_the_allowance_is_a_rolling_day_not_a_calendar_one(self):
        guard = WriteGuard(min_interval=0, daily_cap=2)
        guard.record("0_setpoint", 1000.0)
        guard.record("0_setpoint", 1001.0)
        with pytest.raises(WriteRateExceeded):
            guard.check("0_setpoint", 1002.0)
        # 24 hours after the first two, they no longer count.
        guard.check("0_setpoint", 1000.0 + 86401)

    def test_the_cap_can_be_turned_off(self):
        guard = WriteGuard(min_interval=0, daily_cap=1, enforce_cap=False)
        guard.record("0_setpoint", 1000.0)
        guard.check("0_setpoint", 1001.0)

    def test_spending_is_counted_per_point(self):
        guard = WriteGuard(min_interval=0, daily_cap=1)
        guard.record("0_setpoint", 1000.0)
        guard.check("1_setpoint", 1000.0)

    def test_allows_within_cap_ignores_spacing(self):
        # Coalescing needs to know whether the day's budget is gone without
        # being told that the last write was a moment ago.
        guard = WriteGuard(min_interval=60, daily_cap=5)
        guard.record("0_setpoint", 1000.0)
        assert not guard.allows("0_setpoint", 1005.0)
        assert guard.allows_within_cap("0_setpoint", 1005.0)


class TestWarning:
    def test_warns_once_past_half_the_allowance(self, caplog):
        guard = WriteGuard(min_interval=0, daily_cap=10)
        for tick in range(5):
            guard.record("0_setpoint", 1000.0 + tick)
        assert "over half its daily allowance" in caplog.text
        assert guard.over_half == ["0_setpoint"]

    def test_does_not_warn_on_ordinary_use(self, caplog):
        guard = WriteGuard(min_interval=0, daily_cap=10)
        guard.record("0_setpoint", 1000.0)
        assert "allowance" not in caplog.text
        assert guard.over_half == []

    def test_stops_warning_once_the_day_rolls_past(self):
        guard = WriteGuard(min_interval=0, daily_cap=10)
        for tick in range(6):
            guard.record("0_setpoint", 1000.0 + tick)
        assert guard.over_half == ["0_setpoint"]
        # A day later, with none of the earlier writes still inside the
        # rolling window, the point is back under its allowance.
        guard.record("0_setpoint", 1000.0 + 90000)
        assert guard.over_half == []


class TestReporting:
    def test_summary_lists_the_busiest_points_first(self):
        guard = WriteGuard(min_interval=0)
        for tick in range(3):
            guard.record("0_setpoint", 1000.0 + tick)
        guard.record("1_setpoint", 1000.0)
        assert list(guard.summary(1005.0)) == ["0_setpoint", "1_setpoint"]
        assert guard.summary(1005.0)["0_setpoint"] == 3

    def test_a_quiet_gateway_reports_nothing(self):
        assert WriteGuard().summary(1000.0) == {}
