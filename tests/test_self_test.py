"""The service that says whether it is the gateway or us.

When every point timed out, the only thing that settled it was connecting
to the same gateway from outside Home Assistant: it answered the device
object in 0.6 seconds and read 58 of 64 points, while the integration could
not read one. That comparison is the whole diagnosis, and it should not
require shell access to a container.

So the service tries the live connection, and — only if that fails — opens
a second one on a spare port and tries the same reads again. Two results
side by side name the culprit without ambiguity.
"""

from __future__ import annotations

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.daikin_dbacs import async_setup_entry
from custom_components.daikin_dbacs import gateway as gw
from custom_components.daikin_dbacs.const import (
    CONF_DEVICE_INSTANCE,
    CONF_HOST,
    DOMAIN,
    OPT_MEMBERS,
)

import fake_gateway as fake
from test_services import call, run


@pytest.fixture
def loaded(monkeypatch, tmp_path):
    state = fake.FakeGatewayState(populated={0, 1}, device_instance=12)

    async def connect(self):
        fake.attach(self, state)
        return await self.read_info()

    monkeypatch.setattr(gw.DbacsGateway, "connect", connect)
    hass = HomeAssistant()
    entry = ConfigEntry(
        data={CONF_HOST: "192.0.2.10", CONF_DEVICE_INSTANCE: 12},
        options={OPT_MEMBERS: ["room_temp"]},
        entry_id="entry_twelve",
    )
    hass.config_entries.entries.append(entry)
    run(async_setup_entry(hass, entry))
    return hass, entry, state


class TestAHealthyGateway:
    def test_it_reports_the_live_connection_working(self, loaded):
        hass, _entry, _state = loaded
        report = call(hass, "self_test")

        gateway = report["gateways"][0]
        assert gateway["live"]["answered"] > 0
        assert gateway["verdict"] == "working"

    def test_it_does_not_open_a_second_connection_needlessly(self, loaded):
        hass, _entry, _state = loaded
        report = call(hass, "self_test")
        assert report["gateways"][0].get("fresh") is None

    def test_it_times_the_reads(self, loaded):
        hass, _entry, _state = loaded
        gateway = call(hass, "self_test")["gateways"][0]
        assert "seconds" in gateway["live"]


class TestAGatewayThatHasGoneQuiet:
    def test_a_working_fresh_connection_points_at_us(self, loaded):
        """The live socket is wedged; the gateway is fine.

        Only the application Home Assistant holds is made to time out, so a
        connection opened beside it still reads — which is exactly what was
        observed on gateway 12.
        """
        hass, entry, state = loaded
        state.wedged_apps.add(id(entry.runtime_data.gateway._app))

        gateway = call(hass, "self_test")["gateways"][0]

        assert gateway["live"]["answered"] == 0
        assert gateway["fresh"]["answered"] > 0
        assert gateway["verdict"] == "our end"

    def test_a_failing_fresh_connection_points_at_the_gateway(self, loaded):
        hass, _entry, state = loaded
        state.points_time_out = True
        state.offline = True

        gateway = call(hass, "self_test")["gateways"][0]

        assert gateway["verdict"] == "the gateway"

    def test_the_verdict_is_explained_in_words(self, loaded):
        hass, entry, state = loaded
        state.wedged_apps.add(id(entry.runtime_data.gateway._app))

        gateway = call(hass, "self_test")["gateways"][0]
        assert gateway["advice"]
        assert "new socket" in gateway["advice"]
