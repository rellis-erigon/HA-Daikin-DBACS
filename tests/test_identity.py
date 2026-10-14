"""Reading the gateway's own identity, and refusing to proceed without it.

A gateway whose device object cannot be read is not usable, and the damage
is entirely silent: the mapping file is named after the device instance, so
a wrong or missing instance means no room names, no areas and no outdoor
units, while discovery still finds every unit by probing. The integration
looks like it works. This happened on the second gateway — the entry
recorded instance 0 and every gateway property as None.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.daikin_dbacs import gateway as gw

import fake_gateway as fake


def run(coro):
    return asyncio.run(coro)


class TestAnUnspecifiedInstance:
    def test_zero_means_not_given_and_is_discovered(self):
        """The optional number box submits 0 when it is left empty.

        BACnet allows instance 0, but no D-BACS gateway uses it and the UI
        cannot tell "0" from "blank". Treating it as given is what put a 0
        in a config entry.
        """
        gateway = gw.DbacsGateway("192.0.2.10", device_instance=0)
        state = fake.FakeGatewayState(populated={0}, device_instance=12)
        fake.attach(gateway, state)

        info = run(gateway.read_info())

        assert info.device_instance == 12
        assert gateway.device_instance == 12

    def test_none_is_discovered_as_before(self):
        gateway = gw.DbacsGateway("192.0.2.10", device_instance=None)
        state = fake.FakeGatewayState(populated={0}, device_instance=12)
        fake.attach(gateway, state)

        assert run(gateway.read_info()).device_instance == 12

    def test_an_instance_that_was_given_is_used_as_given(self):
        gateway = gw.DbacsGateway("192.0.2.10", device_instance=12)
        state = fake.FakeGatewayState(populated={0}, device_instance=12)
        fake.attach(gateway, state)

        assert run(gateway.read_info()).device_instance == 12


class TestADeviceObjectThatSaysNothing:
    def test_it_is_an_error_not_an_empty_record(self):
        """Object_Name is a required property of a BACnet device object.

        If it cannot be read, we are not talking to a device at that
        instance, and every later step that depends on the instance will be
        quietly wrong.
        """
        gateway = gw.DbacsGateway("192.0.2.10", device_instance=12)
        state = fake.FakeGatewayState(populated={0}, device_instance=12,
                                      device_object_readable=False)
        fake.attach(gateway, state)

        with pytest.raises(gw.DbacsError) as err:
            run(gateway.read_info())
        assert "12" in str(err.value)

    def test_the_error_says_what_to_do(self):
        gateway = gw.DbacsGateway("192.0.2.10", device_instance=99)
        state = fake.FakeGatewayState(populated={0}, device_instance=12,
                                      device_object_readable=False)
        fake.attach(gateway, state)

        with pytest.raises(gw.DbacsError) as err:
            run(gateway.read_info())
        message = str(err.value).lower()
        assert "instance" in message
        assert "99" in message

    def test_a_readable_device_object_still_works(self):
        gateway = gw.DbacsGateway("192.0.2.10", device_instance=12)
        state = fake.FakeGatewayState(populated={0}, device_instance=12)
        fake.attach(gateway, state)

        info = run(gateway.read_info())
        assert info.object_name


class TestTheEntryRecordsWhatTheGatewaySaid:
    """A wrong instance in the entry must not survive a reload.

    The mapping file is named after the device instance. An entry written
    before the instance was validated carries a wrong one — the second
    gateway here recorded 0 — and re-adding the gateway to fix it would
    discard every name and area set by hand. So setup corrects the entry
    from the gateway's own answer.
    """

    def _setup(self, stored_instance):
        from homeassistant.config_entries import ConfigEntry
        from homeassistant.core import HomeAssistant

        from custom_components.daikin_dbacs import async_setup_entry
        from custom_components.daikin_dbacs.const import (
            CONF_DEVICE_INSTANCE,
            CONF_HOST,
            OPT_MEMBERS,
        )

        state = fake.FakeGatewayState(populated={0, 1}, device_instance=12)

        async def connect(self):
            fake.attach(self, state)
            return await self.read_info()

        import pytest as _pytest  # noqa: PLC0415

        monkey = _pytest.MonkeyPatch()
        monkey.setattr(gw.DbacsGateway, "connect", connect)

        data = {CONF_HOST: "192.0.2.10"}
        if stored_instance is not None:
            data[CONF_DEVICE_INSTANCE] = stored_instance
        entry = ConfigEntry(data=data, options={OPT_MEMBERS: ["room_temp"]})
        hass = HomeAssistant()
        try:
            run(async_setup_entry(hass, entry))
        finally:
            monkey.undo()
        return entry

    def test_a_stored_zero_is_replaced(self):
        from custom_components.daikin_dbacs.const import CONF_DEVICE_INSTANCE

        entry = self._setup(0)
        assert entry.data[CONF_DEVICE_INSTANCE] == 12

    def test_a_missing_instance_is_filled_in(self):
        from custom_components.daikin_dbacs.const import CONF_DEVICE_INSTANCE

        entry = self._setup(None)
        assert entry.data[CONF_DEVICE_INSTANCE] == 12

    def test_a_correct_instance_is_left_alone(self):
        from custom_components.daikin_dbacs.const import CONF_DEVICE_INSTANCE

        entry = self._setup(12)
        assert entry.data[CONF_DEVICE_INSTANCE] == 12
