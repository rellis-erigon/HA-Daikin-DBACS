"""Alarms for units that were here and have stopped answering, or are faulted.

Everything else in this integration goes *unavailable* when its unit stops
answering, which is the honest thing for a reading and the wrong thing for
an alarm: unavailable is grey on a dashboard, triggers nothing that keys on
`on`, and looks the same as a unit that was never there. A unit the gateway
used to publish that has gone quiet is a fault — of the unit, its DIII-NET
wiring, or the outdoor unit it hangs off — and these entities say so.

"Used to publish" means ever, not since the last restart: the groups a
gateway has published live are kept in the entry's data
(`coordinator.known_groups`), so a unit that is dead when Home Assistant
starts still raises its alarm. `daikin_dbacs.recheck` with `forget_missing`
drops units that have been removed on purpose.

Three levels, because they answer different questions:

- each indoor unit: *Not responding*, with the reason;
- each outdoor unit (refrigerant system): *Not responding* when any of its
  indoor units is, and whether it is all of them — every indoor unit on a
  system going quiet at once points at the outdoor unit or its wiring
  rather than at the rooms;
- the gateway: *Units in alarm*, the one entity to hang a notification on —
  every unit not responding or not Normal (in malfunction), listed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .entity import DbacsEntity
from .objects import MEMBERS_BY_KEY, address_label
from .outdoor import SystemEntity


class UnitNotResponding(DbacsEntity, BinarySensorEntity):
    """A known indoor unit that is no longer answering."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, ac_number: int) -> None:
        super().__init__(
            coordinator, ac_number, MEMBERS_BY_KEY["communication_status"]
        )
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{ac_number}_not_responding"
        )
        self._attr_name = "Not responding"
        self._attr_entity_registry_enabled_default = True

    @property
    def available(self) -> bool:
        # The alarm is the one thing that must not go grey with its unit.
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.not_responding(self.ac_number) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "diii_address": address_label(self.ac_number),
            "reason": self.coordinator.not_responding(self.ac_number),
        }


class SystemNotResponding(SystemEntity, BinarySensorEntity):
    """Indoor units on this outdoor unit have stopped answering."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, system_name: str) -> None:
        super().__init__(
            coordinator, system_name, "not_responding", "Not responding"
        )

    def _units(self) -> tuple[list[int], list[int]]:
        """(known units on this system, those of them not answering)."""
        watched = set(self.coordinator.watched_groups)
        known = [
            ac for ac in self.coordinator.systems[self.system_name].ac_numbers
            if ac in watched
        ]
        quiet = [ac for ac in known if self.coordinator.not_responding(ac)]
        return known, quiet

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return bool(self._units()[1])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        known, quiet = self._units()
        return {
            "indoor_units": len(known),
            "not_responding": len(quiet),
            "all_not_responding": bool(known) and len(quiet) == len(known),
            "units_not_responding": [
                f"{self.coordinator.unit_label(ac)}: "
                f"{self.coordinator.not_responding(ac)}"
                for ac in quiet
            ],
        }


class UnitsInAlarm(CoordinatorEntity, BinarySensorEntity):
    """Every unit on this gateway that is not responding or not Normal."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.entry.entry_id
        self._attr_unique_id = f"{entry_id}_units_in_alarm"
        self._attr_name = "Units in alarm"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})

    def _alarms(self) -> tuple[list[str], list[str], list[str]]:
        coordinator = self.coordinator
        quiet, faulted = [], []
        for ac in coordinator.watched_groups:
            reason = coordinator.not_responding(ac)
            if reason:
                quiet.append(f"{coordinator.unit_label(ac)}: {reason}")
                continue
            code = coordinator.malfunction(ac)
            if code:
                faulted.append(f"{coordinator.unit_label(ac)}: {code}")
        outdoor = []
        for name, system in coordinator.systems.items():
            known = [ac for ac in system.ac_numbers
                     if ac in coordinator.watched_groups]
            if known and all(coordinator.not_responding(ac) for ac in known):
                outdoor.append(f"{system.label}: no indoor unit answering")
                continue
            rollup = coordinator.rollup(name)
            if rollup is not None and rollup.faulted:
                outdoor.append(f"{system.label}: {rollup.fault_code or 'Malfunction'}")
        return quiet, faulted, outdoor

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return any(self._alarms())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        quiet, faulted, outdoor = self._alarms()
        return {
            "units_watched": len(self.coordinator.watched_groups),
            "not_responding": len(quiet),
            "in_malfunction": len(faulted),
            "units_not_responding": quiet,
            "units_in_malfunction": faulted,
            "outdoor_units": outdoor,
        }


def binary_sensors(coordinator) -> list[BinarySensorEntity]:
    return (
        [UnitsInAlarm(coordinator)]
        + [SystemNotResponding(coordinator, name) for name in coordinator.systems]
        + [UnitNotResponding(coordinator, ac) for ac in coordinator.watched_groups]
    )
