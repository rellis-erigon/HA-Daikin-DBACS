"""Switches: the commands that are not part of being a thermostat.

Several of these take control away from whoever is in the room. They are off
by default and stay behind an explicit opt-in, with the reason written on
them, because "wall controller disabled" is a maintenance decision rather
than a dashboard convenience.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DbacsConfigEntry
from .entity import DbacsEntity
from .objects import MEMBERS_BY_KEY, Member, PointRef, is_binary_active
from .writes import WriteRefused

#: command member -> the status member that tells the truth about it. A
#: command object reports what was last asked of it, which is not the same
#: thing.
_SWITCHES: dict[str, str | None] = {
    "energy_saving_command": "energy_saving_status",
    "forced_thermo_off_command": "forced_thermo_off_status",
    "system_forced_off": None,
    "remote_lock_on_off": None,
    "remote_lock_mode": None,
    "remote_lock_setpoint": None,
    "central_control_rejection": None,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DbacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if not coordinator.control_enabled:
        return

    entities = []
    for ac_number in coordinator.active_groups:
        group = coordinator.discovery.groups.get(ac_number)
        for key, status_key in _SWITCHES.items():
            member = MEMBERS_BY_KEY[key]
            if group is not None and not group.publishes(member.number):
                continue
            if member.gated and not coordinator.gated_enabled:
                continue
            entities.append(
                DbacsSwitch(coordinator, ac_number, member, status_key)
            )
    async_add_entities(entities)


class DbacsSwitch(DbacsEntity, SwitchEntity):
    """A command point, read from its status counterpart where there is one."""

    def __init__(
        self,
        coordinator,
        ac_number: int,
        member: Member,
        status_key: str | None,
    ) -> None:
        super().__init__(coordinator, ac_number, member)
        self._status_key = status_key
        # Gated points are opt-in at the entry level; once opted in there is
        # no reason to make the user enable each one as well.
        self._attr_entity_registry_enabled_default = True

    @property
    def _state_key(self) -> str:
        key = self._status_key or self.member.key
        return f"{self.ac_number}_{key}"

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        group = self.coordinator.discovery.groups.get(self.ac_number)
        if group is not None and not group.live:
            return False
        reading = (self.coordinator.data or {}).get(self._state_key)
        return reading is not None and reading.ok

    @property
    def is_on(self) -> bool | None:
        reading = (self.coordinator.data or {}).get(self._state_key)
        if reading is None or not reading.ok:
            return None
        return is_binary_active(reading.value)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attributes = super().extra_state_attributes or {}
        if self._status_key:
            attributes["read_from"] = MEMBERS_BY_KEY[self._status_key].object_name
        if self.member.gated:
            attributes["affects_occupants"] = True
        return attributes

    async def _write(self, value: str) -> None:
        point = PointRef(self.ac_number, self.member)
        try:
            await self.coordinator.async_write(point, value)
        except WriteRefused as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write("active")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write("inactive")
