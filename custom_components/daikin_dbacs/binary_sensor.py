"""Binary sensors: the points that really are on or off."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DbacsConfigEntry
from .entity import DbacsEntity
from .outdoor import binary_sensors as system_entities
from .objects import Kind, Member, ObjType, READABLE, is_binary_active

#: Every readable member; the coordinator narrows it per group.
_ALL_KEYS = tuple(member.key for member in READABLE)

#: Which binary points carry a device class, and which are just states.
_DEVICE_CLASSES: dict[str, BinarySensorDeviceClass] = {
    "on_off_status": BinarySensorDeviceClass.RUNNING,
    "alarm": BinarySensorDeviceClass.PROBLEM,
    "filter_sign": BinarySensorDeviceClass.PROBLEM,
    "communication_status": BinarySensorDeviceClass.PROBLEM,
    "indoor_fan_status": BinarySensorDeviceClass.RUNNING,
    "compressor_status": BinarySensorDeviceClass.RUNNING,
    "heater_status": BinarySensorDeviceClass.RUNNING,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DbacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One binary sensor per binary status point a live group publishes."""
    coordinator = entry.runtime_data
    # Outdoor units first: on a dashboard the thing that breaks belongs
    # above the ninety-six things that report it.
    async_add_entities(system_entities(coordinator))
    async_add_entities(
        DbacsBinarySensor(coordinator, point.ac_number, point.member)
        for ac_number in coordinator.active_groups
        for point in coordinator.points_of(ac_number, _ALL_KEYS)
        if _is_binary_sensor(point.member)
    )


def _is_binary_sensor(member: Member) -> bool:
    if member.object_type not in (ObjType.BI, ObjType.BO, ObjType.BV):
        return False
    # Command objects report what was last asked of them, not what the unit
    # is doing. Their status counterparts are the ones worth showing.
    return member.kind is Kind.STATUS


class DbacsBinarySensor(DbacsEntity, BinarySensorEntity):
    """A binary point."""

    def __init__(self, coordinator, ac_number: int, member: Member) -> None:
        super().__init__(coordinator, ac_number, member)
        device_class = _DEVICE_CLASSES.get(member.key)
        if device_class is not None:
            self._attr_device_class = device_class

    @property
    def is_on(self) -> bool | None:
        reading = self.reading
        if reading is None or not reading.ok:
            return None
        return is_binary_active(reading.value)

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        attributes = super().extra_state_attributes or {}
        reading = self.reading
        if reading is not None and reading.ok and self.member.states:
            from .objects import member_state  # noqa: PLC0415

            label = member_state(self.member, reading.value)
            if label:
                attributes["state_label"] = label
        return attributes
