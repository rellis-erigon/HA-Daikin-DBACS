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
    """Everything that is not a binary point, and not a command.

    A command object's value is readable, but it says what was last asked
    for rather than what the unit is doing — and on an analog command such
    as air direction the two differ visibly. The matching status object is
    the honest answer, so a command is never a sensor; the climate entity
    owns it instead.

    This used to exclude only binary and multistate commands, which let
    AirDirectionCommand through as a sensor on every group.
    """
    if member.kind is Kind.COMMAND:
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
        elif member.key == "malfunction_code":
            pass  # free text: a Daikin code, or Normal
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

        if self.member.key == "malfunction_code":
            return self._fault_state(reading.value)

        if self.member.unit == "temperature":
            # The gateway sends float32, so a room temperature arrives as
            # 27.2000007629395. Rounded here rather than only in the display
            # precision, because the raw value is what gets recorded.
            try:
                return round(float(reading.value), 1)
            except (TypeError, ValueError):
                return None

        if self.member.unit in ("energy", "volume"):
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

    def _fault_state(self, value: object) -> str | None:
        """The Daikin fault code, which is the point of this sensor.

        The object's own value is an index into a table Daikin publishes
        separately; the two-character code lives in the object's Description
        and is what an engineer actually needs. The index stays available as
        an attribute.
        """
        code = self.coordinator.fault_code(self.ac_number)
        if code:
            return code
        try:
            index = int(value)
        except (TypeError, ValueError):
            return None
        return "Normal" if index <= 1 else str(index)

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
