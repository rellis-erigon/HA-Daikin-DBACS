"""Setting the entry up and tearing it down."""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.daikin_dbacs import (
    async_setup_entry,
    async_unload_entry,
    diagnostics,
)
from custom_components.daikin_dbacs import gateway as gw
from custom_components.daikin_dbacs.const import CONF_HOST, OPT_MEMBERS

import fake_gateway as fake


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def connects(monkeypatch):
    state = fake.FakeGatewayState(populated={0, 1, 64}, faulted={1})

    async def connect(self):
        fake.attach(self, state)
        return await self.read_info()

    monkeypatch.setattr(gw.DbacsGateway, "connect", connect)
    return state


def entry_for(options=None):
    return ConfigEntry(
        data={CONF_HOST: "192.0.2.10"},
        options=options or {OPT_MEMBERS: ["room_temp", "alarm", "malfunction_code"]},
    )


class TestSetup:
    def test_sets_up_and_discovers(self, connects):
        hass = HomeAssistant()
        entry = entry_for()
        assert run(async_setup_entry(hass, entry))

        coordinator = entry.runtime_data
        assert coordinator.active_groups == (0, 1, 64)
        assert coordinator.data
        assert hass.config_entries.forwarded

    def test_an_unreachable_gateway_defers_rather_than_half_loading(
        self, monkeypatch
    ):
        async def connect(self):
            raise gw.DbacsUnreachable("no answer")

        monkeypatch.setattr(gw.DbacsGateway, "connect", connect)
        with pytest.raises(ConfigEntryNotReady):
            run(async_setup_entry(HomeAssistant(), entry_for()))

    def test_a_gateway_still_scanning_defers_too(self, monkeypatch):
        async def connect(self):
            raise gw.DbacsNotReady("configuration-in-progress")

        monkeypatch.setattr(gw.DbacsGateway, "connect", connect)
        # Home Assistant will retry, which is exactly the right behaviour for
        # a gateway that is simply not finished booting.
        with pytest.raises(ConfigEntryNotReady):
            run(async_setup_entry(HomeAssistant(), entry_for()))

    def test_unloading_gives_the_gateway_its_slots_back(self, connects):
        hass = HomeAssistant()
        entry = entry_for()
        run(async_setup_entry(hass, entry))
        coordinator = entry.runtime_data

        assert run(async_unload_entry(hass, entry))
        assert not coordinator.gateway.connected
        assert coordinator.gateway.subscription_count == 0


class TestDiagnostics:
    def test_tells_a_quiet_gateway_from_an_empty_one(self, connects):
        hass = HomeAssistant()
        entry = entry_for()
        run(async_setup_entry(hass, entry))

        report = run(
            diagnostics.async_get_config_entry_diagnostics(hass, entry)
        )

        assert report["gateway"]["model_name"] == "DMS502B71"
        assert report["gateway"]["max_apdu_length"] == 1024
        assert report["discovery"]["live_count"] == 3
        assert report["discovery"]["live"] == [
            "1 · 1-00",
            "1 · 1-01",
            "2 · 1-00",
        ]
        assert report["discovery"]["absent_count"] == 253
        assert report["points"]["requested"] == 9
        assert report["points"]["errored"] == 0
        assert report["fault_codes"] == {"1 · 1-01": "U1"}

    def test_reports_dead_groups_separately_from_absent_ones(
        self, monkeypatch
    ):
        state = fake.FakeGatewayState(populated={0, 1}, dead={1})

        async def connect(self):
            fake.attach(self, state)
            return await self.read_info()

        monkeypatch.setattr(gw.DbacsGateway, "connect", connect)
        hass = HomeAssistant()
        entry = entry_for()
        run(async_setup_entry(hass, entry))

        report = run(
            diagnostics.async_get_config_entry_diagnostics(hass, entry)
        )
        assert report["discovery"]["dead"] == ["1 · 1-01"]
        assert report["discovery"]["live"] == ["1 · 1-00"]
