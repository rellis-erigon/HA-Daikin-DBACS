"""Two gateways must not share one BACnet socket or one device identity.

Both config entries defaulted to local port 47808 and our_instance
4194300. In one Home Assistant process that means two bacpypes3
applications bound to the same UDP port and two local devices claiming the
same BACnet instance number. Replies are delivered to whichever socket the
operating system picks: on this site gateway 11 received everything and
gateway 12 timed out on all 2,016 of its points, while a fresh connection
from outside Home Assistant read the same gateway in 0.6 seconds.

The entry still said `loaded`, so nothing looked wrong except that half the
estate read `unavailable`.
"""

from __future__ import annotations

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.daikin_dbacs import _local_identity
from custom_components.daikin_dbacs.const import (
    CONF_HOST,
    CONF_LOCAL_PORT,
    CONF_OUR_INSTANCE,
    DEFAULT_LOCAL_PORT,
    DEFAULT_OUR_INSTANCE,
)


def entry_for(host, entry_id, **data):
    return ConfigEntry(data={CONF_HOST: host, **data}, entry_id=entry_id)


@pytest.fixture
def hass():
    return HomeAssistant()


class TestTheFirstGatewayKeepsTheDefaults:
    def test_nothing_is_moved_when_it_is_alone(self, hass):
        entry = entry_for("192.0.2.10", "one")
        hass.config_entries.entries.append(entry)

        port, instance = _local_identity(hass, entry)

        assert port == DEFAULT_LOCAL_PORT
        assert instance == DEFAULT_OUR_INSTANCE


class TestASecondGatewayIsMovedAside:
    def _two(self, hass):
        first = entry_for("192.0.2.10", "one",
                          **{CONF_LOCAL_PORT: DEFAULT_LOCAL_PORT,
                             CONF_OUR_INSTANCE: DEFAULT_OUR_INSTANCE})
        second = entry_for("192.0.2.11", "two")
        hass.config_entries.entries += [first, second]
        return first, second

    def test_it_gets_a_different_port(self, hass):
        _first, second = self._two(hass)
        port, _instance = _local_identity(hass, second)
        assert port != DEFAULT_LOCAL_PORT

    def test_it_gets_a_different_device_instance(self, hass):
        _first, second = self._two(hass)
        _port, instance = _local_identity(hass, second)
        assert instance != DEFAULT_OUR_INSTANCE

    def test_the_instance_is_still_a_legal_one(self, hass):
        """4194303 is the unconfigured address and must not be claimed."""
        _first, second = self._two(hass)
        _port, instance = _local_identity(hass, second)
        assert 0 <= instance < 4194303

    def test_a_third_gateway_avoids_both(self, hass):
        first, second = self._two(hass)
        port2, instance2 = _local_identity(hass, second)
        second.data = {**second.data, CONF_LOCAL_PORT: port2,
                       CONF_OUR_INSTANCE: instance2}
        third = entry_for("192.0.2.12", "three")
        hass.config_entries.entries.append(third)

        port3, instance3 = _local_identity(hass, third)
        assert port3 not in (DEFAULT_LOCAL_PORT, port2)
        assert instance3 not in (DEFAULT_OUR_INSTANCE, instance2)


class TestAnEntryAlreadyWrittenWithTheDefaults:
    """The config flow writes both values into every entry it creates.

    So the pair cannot be read as "the user asked for this" — which means
    an entry that is already wrong has to be corrected, not respected.
    This is the case the live system was in.
    """

    def test_the_colliding_pair_is_corrected(self, hass):
        first = entry_for("192.0.2.10", "one",
                          **{CONF_LOCAL_PORT: DEFAULT_LOCAL_PORT,
                             CONF_OUR_INSTANCE: DEFAULT_OUR_INSTANCE})
        second = entry_for("192.0.2.11", "two",
                           **{CONF_LOCAL_PORT: DEFAULT_LOCAL_PORT,
                              CONF_OUR_INSTANCE: DEFAULT_OUR_INSTANCE})
        hass.config_entries.entries += [first, second]

        assert _local_identity(hass, first) == (DEFAULT_LOCAL_PORT,
                                                DEFAULT_OUR_INSTANCE)
        port, instance = _local_identity(hass, second)
        assert port != DEFAULT_LOCAL_PORT
        assert instance != DEFAULT_OUR_INSTANCE

    def test_a_port_nobody_else_uses_is_left_alone(self, hass):
        first = entry_for("192.0.2.10", "one",
                          **{CONF_LOCAL_PORT: DEFAULT_LOCAL_PORT,
                             CONF_OUR_INSTANCE: DEFAULT_OUR_INSTANCE})
        second = entry_for("192.0.2.11", "two",
                           **{CONF_LOCAL_PORT: 47900,
                              CONF_OUR_INSTANCE: 4194200})
        hass.config_entries.entries += [first, second]

        assert _local_identity(hass, second) == (47900, 4194200)

    def test_an_entry_does_not_collide_with_itself(self, hass):
        entry = entry_for("192.0.2.10", "one",
                          **{CONF_LOCAL_PORT: DEFAULT_LOCAL_PORT,
                             CONF_OUR_INSTANCE: DEFAULT_OUR_INSTANCE})
        hass.config_entries.entries.append(entry)

        port, instance = _local_identity(hass, entry)

        assert (port, instance) == (DEFAULT_LOCAL_PORT, DEFAULT_OUR_INSTANCE)
