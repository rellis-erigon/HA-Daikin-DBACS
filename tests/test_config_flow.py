"""Adding a gateway, and choosing what it carries."""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.daikin_dbacs import config_flow as cf
from custom_components.daikin_dbacs import gateway as gw
from custom_components.daikin_dbacs.const import (
    CONF_DEVICE_INSTANCE,
    CONF_HOST,
    CONF_LOCAL_PORT,
    CONF_OUR_INSTANCE,
    CONF_PORT,
    DEFAULT_LOCAL_PORT,
    DEFAULT_OUR_INSTANCE,
    DEFAULT_PORT,
    OPT_GROUPS,
    OPT_MEMBERS,
    OPT_SCAN_INTERVAL,
)
from custom_components.daikin_dbacs.coordinator import DbacsCoordinator
from custom_components.daikin_dbacs.objects import DEFAULT_MEMBERS

import fake_gateway as fake


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def connects(monkeypatch):
    """Let connect() succeed against a synthetic gateway, without binding."""
    state = fake.FakeGatewayState()

    async def connect(self):
        fake.attach(self, state)
        return await self.read_info()

    async def close(self):
        return None

    monkeypatch.setattr(gw.DbacsGateway, "connect", connect)
    monkeypatch.setattr(gw.DbacsGateway, "close", close)
    return state


@pytest.fixture
def fails(monkeypatch):
    def _fail(error):
        async def connect(self):
            raise error

        monkeypatch.setattr(gw.DbacsGateway, "connect", connect)

    return _fail


class TestAddingAGateway:
    def test_shows_a_form_first(self):
        result = run(cf.DbacsConfigFlow().async_step_user())
        assert result["type"] == "form"
        assert result["step_id"] == "user"

    def test_creates_the_entry_when_the_gateway_answers(self, connects):
        flow = cf.DbacsConfigFlow()
        result = run(flow.async_step_user({CONF_HOST: "192.0.2.10"}))

        assert result["type"] == "create_entry"
        assert result["title"] == "D-BACS-TEST"
        assert result["data"][CONF_HOST] == "192.0.2.10"
        assert result["data"][CONF_PORT] == DEFAULT_PORT
        assert result["data"][CONF_LOCAL_PORT] == DEFAULT_LOCAL_PORT
        assert result["data"][CONF_OUR_INSTANCE] == DEFAULT_OUR_INSTANCE
        assert flow.unique_id == "192.0.2.10:47808"

    def test_records_the_device_instance_it_learned(self, connects):
        result = run(
            cf.DbacsConfigFlow().async_step_user({CONF_HOST: "192.0.2.10"})
        )
        # Learned from a unicast Who-Is, so the next start does not have to
        # ask again.
        assert result["data"][CONF_DEVICE_INSTANCE] == connects.device_instance

    def test_starts_with_the_default_points_enabled(self, connects):
        result = run(
            cf.DbacsConfigFlow().async_step_user({CONF_HOST: "192.0.2.10"})
        )
        assert result["options"][OPT_MEMBERS] == list(DEFAULT_MEMBERS)

    def test_an_unreachable_gateway_fails_the_form_with_the_reason(self, fails):
        fails(gw.DbacsUnreachable("no I-Am from 192.0.2.10:47808"))
        result = run(
            cf.DbacsConfigFlow().async_step_user({CONF_HOST: "192.0.2.10"})
        )
        assert result["type"] == "form"
        assert result["errors"]["base"] == "cannot_connect"
        assert "no I-Am" in result["description_placeholders"]["error"]

    def test_a_gateway_still_scanning_is_its_own_message(self, fails):
        # "Try again in a minute" is a different instruction from "check the
        # address", so it gets a different error.
        fails(gw.DbacsNotReady("device/configuration-in-progress"))
        result = run(
            cf.DbacsConfigFlow().async_step_user({CONF_HOST: "192.0.2.10"})
        )
        assert result["errors"]["base"] == "scanning"

    def test_an_unexpected_failure_is_surfaced_not_hidden(self, fails):
        fails(RuntimeError("something nobody predicted"))
        result = run(
            cf.DbacsConfigFlow().async_step_user({CONF_HOST: "192.0.2.10"})
        )
        assert result["errors"]["base"] == "unknown"
        assert "nobody predicted" in result["description_placeholders"]["error"]

    def test_form_numbers_are_stored_as_integers(self, connects):
        result = run(
            cf.DbacsConfigFlow().async_step_user(
                {
                    CONF_HOST: " 192.0.2.10 ",
                    CONF_PORT: 47809.0,
                    CONF_LOCAL_PORT: 47810.0,
                    CONF_OUR_INSTANCE: 4194299.0,
                }
            )
        )
        data = result["data"]
        assert data[CONF_HOST] == "192.0.2.10"
        assert data[CONF_PORT] == 47809
        assert isinstance(data[CONF_PORT], int)
        assert data[CONF_LOCAL_PORT] == 47810
        assert data[CONF_OUR_INSTANCE] == 4194299


class TestChoosingPoints:
    def _flow(self, options=None, coordinator=None):
        flow = cf.DbacsOptionsFlow()
        entry = ConfigEntry(options=options or {})
        entry.runtime_data = coordinator
        flow.config_entry = entry
        return flow

    def test_offers_every_readable_member(self):
        result = run(self._flow().async_step_init())
        assert result["type"] == "form"

    def test_offers_only_the_groups_that_exist(self):
        state = fake.FakeGatewayState(populated={0, 1, 64})
        client = gw.DbacsGateway("192.0.2.10")
        fake.attach(client, state)
        info = run(client.read_info())
        coordinator = DbacsCoordinator(
            HomeAssistant(), ConfigEntry(), client, info
        )
        run(coordinator.async_discover())

        # Offering all 256 slots when three are wired would be a worse form
        # than offering none.
        assert [
            option["label"] for option in cf._group_options(coordinator)
        ] == ["1 · 1-00", "1 · 1-01", "2 · 1-00"]

    def test_offers_no_groups_before_discovery_has_run(self):
        assert cf._group_options(None) == []

    def test_saves_what_was_chosen(self):
        flow = self._flow()
        result = run(
            flow.async_step_init(
                {
                    OPT_MEMBERS: ["room_temp", "setpoint"],
                    OPT_GROUPS: ["0", "64"],
                    OPT_SCAN_INTERVAL: 120.0,
                }
            )
        )
        assert result["type"] == "create_entry"
        assert result["data"][OPT_MEMBERS] == ["room_temp", "setpoint"]
        # Groups come back from the picker as strings and have to be stored
        # as the numbers the rest of the integration indexes by.
        assert result["data"][OPT_GROUPS] == [0, 64]
        assert result["data"][OPT_SCAN_INTERVAL] == 120
        assert isinstance(result["data"][OPT_SCAN_INTERVAL], int)
