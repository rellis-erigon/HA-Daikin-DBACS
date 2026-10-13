"""Buttons: the points whose only meaning is being pressed."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DbacsConfigEntry
from .entity import DbacsEntity
from .objects import MEMBERS_BY_KEY, Kind, PointRef
from .writes import WriteRefused

#: Reset-style members. They have no state worth reading — the matching sign
#: object says whether the condition is there — so they are buttons rather
#: than switches.
_BUTTONS = ("filter_sign_reset",)


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
        for key in _BUTTONS:
            member = MEMBERS_BY_KEY[key]
            if group is not None and not group.publishes(member.number):
                continue
            entities.append(DbacsButton(coordinator, ac_number, member))
    async_add_entities(entities)


class DbacsButton(DbacsEntity, ButtonEntity):
    """A reset."""

    def __init__(self, coordinator, ac_number: int, member) -> None:
        super().__init__(coordinator, ac_number, member)
        assert member.kind is Kind.RESET
        self._attr_entity_registry_enabled_default = True

    @property
    def available(self) -> bool:
        # A reset has nothing to read, so it is available whenever its group
        # is — not when a reading for it exists, because there never is one.
        if not self.coordinator.last_update_success:
            return False
        group = self.coordinator.discovery.groups.get(self.ac_number)
        return group is None or group.live

    async def async_press(self) -> None:
        point = PointRef(self.ac_number, self.member)
        try:
            await self.coordinator.async_write(point, "active")
        except WriteRefused as err:
            raise HomeAssistantError(str(err)) from err
