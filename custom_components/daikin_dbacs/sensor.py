"""Sensors: the readings that are not simply on or off."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DbacsConfigEntry
from .entity import DbacsEntity
from .objects import Kind, Member, ObjType, READABLE, member_state

#: Every readable member, so the platform sees the full candidate list and
#: the coordinator narrows it to what the group publishes.
_ALL_KEYS = tuple(member.key for member in READABLE)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DbacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One sensor per non-binary point every live group publishes.

    Built from the group's own point list rather than from the member table,
    because groups genuinely differ in what they carry. An entity for an
    object the gateway does not have could only ever be unavailable.
    """
    coordinator = entry.runtime_data
    async_add_entities(
        DbacsSensor(coordinator, point.ac_number, point.member)
        for ac_number in coordinator.active_groups
        for point in coordinator.points_of(ac_number, _ALL_KEYS)
        if _is_sensor(point.member)
    )


def _is_sensor(member: Member) -> bool:
    """Everything that is not a binary point, and not write-only."""
    if member.kind is Kind.COMMAND and member.object_type in (
        ObjType.BO,
        ObjType.BV,
        ObjType.MO,
    ):
        # A command object's own value is readable, but it says what was last
        # asked for rather than what the unit is doing. The matching status
        # object is the honest answer, so the command is not exposed until
        # there is a control entity that owns it.
        return False
    return member.object_type not in (ObjType.BI, ObjType.BO, ObjType.BV)


class DbacsSensor(DbacsEntity, SensorEntity):
    """A numeric or enumerated point."""

    def __init__(self, coordinator, ac_number: int, member: Member) -> None:
        super().__init__(coordinator, ac_number, member)

        if member.unit == "temperature":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 1
        elif member.unit == "energy":
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        elif member.unit == "volume":
            # No device class: what this accumulator counts is inferred
            # rather than documented, and a wrong device class would put it
            # on the energy dashboard as a fact.
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        elif member.states:
            self._attr_device_class = SensorDeviceClass.ENUM
            # Daikin leaves a documented hole in the mode enumeration, and a
            # state the gateway never sends does not belong in the options.
            self._attr_options = [
                state for state in member.states if state is not None
            ]

    @property
    def native_value(self) -> float | str | None:
        reading = self.reading
        if reading is None or not reading.ok:
            return None
        if self.member.unit in ("temperature", "energy"):
            try:
                return float(reading.value)
            except (TypeError, ValueError):
                return None
        labelled = member_state(self.member, reading.value)
        if labelled is not None:
            return labelled
        try:
            return float(reading.value)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        attributes = super().extra_state_attributes or {}
        if self.member.key == "malfunction_code":
            code = self.coordinator.fault_code(self.ac_number)
            if code:
                attributes["fault_code"] = code
            reading = self.reading
            if reading is not None and reading.ok:
                attributes["raw_value"] = str(reading.value)
        return attributes
