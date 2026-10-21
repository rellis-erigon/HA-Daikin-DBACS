"""Rated capacity from a Daikin indoor model number."""

from custom_components.daikin_dbacs.capacity import Capacity, capacity_of


def test_reads_the_class_from_each_series():
    assert capacity_of("FXDQ40TV1B") == Capacity(4.5, 5.0)
    assert capacity_of("FXDQ32PTV1B") == Capacity(3.6, 4.0)
    assert capacity_of("FXMQ250PV1A") == Capacity(28.0, 31.5)
    assert capacity_of("FXFQ25PVE8") == Capacity(2.8, 3.2)
    assert capacity_of("fxmq140pave") == Capacity(16.0, 18.0)


def test_not_an_indoor_model():
    assert capacity_of(None) is None
    assert capacity_of("") is None
    assert capacity_of("REYQ16TAY1") is None       # an outdoor unit
    assert capacity_of("FXDQ99TV1B") is None       # no such class


class TestTheModelReachesTheUnit:
    """A model in the site mapping names the device and rates the unit."""

    def _build(self):
        from test_control import build, collect
        from custom_components.daikin_dbacs import climate
        from custom_components.daikin_dbacs.mapping import UnitMapping

        coordinator, entry, _state, hass = build()
        coordinator.site = {0: UnitMapping(0, model="FXDQ50TV1B")}
        return {e.ac_number: e for e in collect(climate, entry, hass)}

    def test_a_mapped_unit_carries_its_model_and_duty(self):
        unit = self._build()[0]
        assert unit.device_info["model"] == "FXDQ50TV1B"
        attributes = unit.extra_state_attributes
        assert attributes["model"] == "FXDQ50TV1B"
        assert attributes["rated_cooling_kw"] == 5.6
        assert attributes["rated_heating_kw"] == 6.3

    def test_an_unmapped_unit_claims_no_duty(self):
        unit = self._build()[1]
        assert unit.device_info["model"] == "Interface for use in BACnet"
        assert "rated_cooling_kw" not in unit.extra_state_attributes
