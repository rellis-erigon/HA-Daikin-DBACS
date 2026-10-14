"""A sweep where nothing answers is a failure, not a success.

`read_points` reports a per-point failure by returning a reading that is
not ok, because one dead unit among 96 must not fail the whole sweep. The
consequence, unnoticed until a second gateway was added: when *every* point
failed the sweep still counted as successful. The coordinator's
`last_update_success` stayed true, the config entry reported `loaded`,
nothing was written to the log, and all 2,142 of that gateway's entities sat
`unavailable` for over two hours.

The gateway was fine throughout — it answered a batch of 64 points in 0.6
seconds from outside Home Assistant. Only the integration's socket was
wedged, and nothing in the integration would ever have retried it.
"""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.daikin_dbacs import gateway as gw

import fake_gateway as fake
from test_coordinator import build, run


class TestNothingAnswered:
    def test_the_sweep_fails_instead_of_passing(self):
        state = fake.FakeGatewayState(populated={0, 1})
        coordinator, _ = build(state)
        run(coordinator.async_discover())

        state.points_time_out = True
        with pytest.raises(UpdateFailed) as err:
            run(coordinator._async_update_data())
        assert "answered" in str(err.value)

    def test_a_partly_answering_gateway_still_passes(self):
        """One dead unit among many must not fail the sweep."""
        state = fake.FakeGatewayState(populated={0, 1}, broken={2})
        coordinator, _ = build(state)
        run(coordinator.async_discover())

        data = run(coordinator._async_update_data())
        assert any(r.ok for r in data.values())

    def test_nothing_to_read_is_not_a_dead_gateway(self):
        """No points selected must not look like a gateway that went quiet."""
        state = fake.FakeGatewayState(populated={0})
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        coordinator.points = lambda: []

        assert run(coordinator._async_update_data()) == {}
        assert coordinator._dead_sweeps == 0


class TestItReconnectsRatherThanSittingThere:
    def _wedged(self):
        state = fake.FakeGatewayState(populated={0, 1})
        coordinator, _ = build(state)
        run(coordinator.async_discover())
        state.points_time_out = True
        return coordinator, state

    def test_one_dead_sweep_does_not_reconnect(self):
        """A single bad sweep is a blip, not a wedged socket."""
        coordinator, _state = self._wedged()
        reconnects = []
        coordinator.gateway.reconnect = lambda: reconnects.append(1)

        with pytest.raises(UpdateFailed):
            run(coordinator._async_update_data())
        assert reconnects == []

    def test_repeated_dead_sweeps_reconnect_the_stack(self):
        coordinator, _state = self._wedged()
        reconnects = []

        async def reconnect():
            reconnects.append(1)

        coordinator.gateway.reconnect = reconnect

        for _ in range(coordinator.DEAD_SWEEPS_BEFORE_RECONNECT):
            with pytest.raises(UpdateFailed):
                run(coordinator._async_update_data())

        assert len(reconnects) == 1

    def test_a_good_sweep_clears_the_count(self):
        coordinator, state = self._wedged()
        reconnects = []

        async def reconnect():
            reconnects.append(1)

        coordinator.gateway.reconnect = reconnect

        with pytest.raises(UpdateFailed):
            run(coordinator._async_update_data())
        state.points_time_out = False
        run(coordinator._async_update_data())
        state.points_time_out = True
        with pytest.raises(UpdateFailed):
            run(coordinator._async_update_data())

        assert reconnects == []

    def test_a_reconnect_that_fails_is_reported_not_swallowed(self):
        coordinator, _state = self._wedged()

        async def reconnect():
            raise gw.DbacsUnreachable("still unreachable")

        coordinator.gateway.reconnect = reconnect

        for _ in range(coordinator.DEAD_SWEEPS_BEFORE_RECONNECT):
            with pytest.raises(UpdateFailed):
                run(coordinator._async_update_data())


class TestTheGatewayCanReconnect:
    def test_it_replaces_the_application(self):
        state = fake.FakeGatewayState(populated={0})
        gateway = gw.DbacsGateway("192.0.2.10", device_instance=12)
        fake.attach(gateway, state)
        before = gateway._app

        async def connect(self):
            fake.attach(self, state)
            return await self.read_info()

        gw.DbacsGateway.connect = connect
        try:
            asyncio.run(gateway.reconnect())
        finally:
            del gw.DbacsGateway.connect
        assert gateway._app is not before
