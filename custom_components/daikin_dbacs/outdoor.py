"""The outdoor-unit entities, built from the indoor units on each system.

A device per refrigerant system, because that is the thing that breaks. See
systems.py for why this has to be a roll-up rather than a read.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .codes import meaning
from .const import DOMAIN, MANUFACTURER


class SystemEntity(CoordinatorEntity):
    """Something about one refrigerant system."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, system_name: str, key: str, name: str) -> None:
        super().__init__(coordinator)
        self.system_name = system_name
        self._key = key
        system = coordinator.systems[system_name]

        entry_id = coordinator.entry.entry_id
        self._attr_unique_id = f"{entry_id}_system_{system_name}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_system_{system_name}")},
            name=system.label,
            manufacturer=MANUFACTURER,
            model="Outdoor unit (derived from its indoor units)",
            serial_number=system.name,
        )
        if coordinator.hub_device_id is not None:
            self._attr_device_info["via_device_id"] = coordinator.hub_device_id

    @property
    def rollup(self):
        return self.coordinator.rollup(self.system_name)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        rollup = self.rollup
        return bool(rollup and rollup.answering)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rollup = self.rollup
        if rollup is None:
            return None
        attributes = rollup.as_dict()
        attributes["indoor_addresses"] = list(rollup.system.addresses)
        return attributes


class SystemCompressor(SystemEntity, BinarySensorEntity):
    """Whether the outdoor unit's compressor is running."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator, system_name: str) -> None:
        super().__init__(coordinator, system_name, "compressor", "Compressor")

    @property
    def is_on(self) -> bool | None:
        rollup = self.rollup
        return None if rollup is None else rollup.compressor_running


class SystemProblem(SystemEntity, BinarySensorEntity):
    """Whether any indoor unit on this system is in alarm."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, system_name: str) -> None:
        super().__init__(coordinator, system_name, "problem", "Alarm")

    @property
    def is_on(self) -> bool | None:
        rollup = self.rollup
        return None if rollup is None else rollup.faulted


class SystemNotDelivering(SystemEntity, BinarySensorEntity):
    """Units are calling for something and the compressor is not running.

    The entity this whole module exists for. An outdoor unit that has
    stopped does not announce itself: only the indoor units that happen to
    be calling raise a code, and the rest read Normal while being just as
    badly served.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, system_name: str) -> None:
        super().__init__(
            coordinator, system_name, "not_delivering", "Not delivering"
        )

    @property
    def is_on(self) -> bool | None:
        rollup = self.rollup
        return None if rollup is None else rollup.not_delivering


class SystemCount(SystemEntity, SensorEntity):
    """A count of indoor units in some state."""

    def __init__(
        self, coordinator, system_name: str, key: str, name: str, icon: str
    ) -> None:
        super().__init__(coordinator, system_name, key, name)
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = "units"

    @property
    def native_value(self) -> int | None:
        rollup = self.rollup
        return None if rollup is None else getattr(rollup, self._key, None)


class SystemFaultCode(SystemEntity, SensorEntity):
    """The code the units on this system agree on, where they do."""

    def __init__(self, coordinator, system_name: str) -> None:
        super().__init__(coordinator, system_name, "fault_code", "Fault code")

    @property
    def native_value(self) -> str | None:
        rollup = self.rollup
        if rollup is None:
            return None
        return rollup.fault_code or "Normal"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attributes = super().extra_state_attributes or {}
        rollup = self.rollup
        if rollup is not None and rollup.fault_code:
            meanings = [m for code in sorted(set(rollup.codes))
                        if (m := meaning(code))]
            if meanings:
                attributes["meaning"] = "; ".join(meanings)
        return attributes


def binary_sensors(coordinator) -> list[SystemEntity]:
    return [
        cls(coordinator, name)
        for name in coordinator.systems
        for cls in (SystemCompressor, SystemProblem, SystemNotDelivering)
    ]


def sensors(coordinator) -> list[SystemEntity]:
    out: list[SystemEntity] = []
    for name in coordinator.systems:
        out.append(SystemFaultCode(coordinator, name))
        out.append(SystemCount(coordinator, name, "running", "Units running",
                               "mdi:air-conditioner"))
        out.append(SystemCount(coordinator, name, "calling", "Units calling",
                               "mdi:thermometer-alert"))
        out.append(SystemCount(coordinator, name, "indoor_total",
                               "Indoor units", "mdi:counter"))
    return out
