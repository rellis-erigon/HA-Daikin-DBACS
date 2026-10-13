"""A fault event per group, so history shows when rather than only whether.

An alarm binary sensor tells you a unit is faulted now. It does not tell you
that it faulted at 04:10, cleared at 04:40 and did it again an hour later,
which is the shape of an intermittent fault and the thing worth catching.
"""

from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DbacsConfigEntry
from .entity import DbacsEntity
from .objects import MEMBERS_BY_KEY, is_binary_active

_LOGGER = logging.getLogger(__name__)

EVENT_FAULT = "fault"
EVENT_CLEARED = "cleared"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DbacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    alarm = MEMBERS_BY_KEY["alarm"]
    async_add_entities(
        DbacsFaultEvent(coordinator, ac_number)
        for ac_number in coordinator.active_groups
        if (group := coordinator.discovery.groups.get(ac_number)) is None
        or group.publishes(alarm.number)
    )


class DbacsFaultEvent(DbacsEntity, EventEntity):
    """Fires when a group's alarm goes on or off."""

    _attr_event_types = [EVENT_FAULT, EVENT_CLEARED]

    def __init__(self, coordinator, ac_number: int) -> None:
        super().__init__(coordinator, ac_number, MEMBERS_BY_KEY["alarm"])
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{ac_number}_fault_event"
        )
        self._attr_name = "Fault"
        self._attr_entity_registry_enabled_default = True
        self._faulted: bool | None = None

    @property
    def available(self) -> bool:
        # An event entity holds the last thing that happened, so it stays
        # available even when this sweep could not read the alarm.
        group = self.coordinator.discovery.groups.get(self.ac_number)
        return group is None or group.live

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Record where we came in, so the first sweep after a restart does
        # not announce every already-faulted unit as a new fault.
        self._faulted = self._read_faulted()

    def _read_faulted(self) -> bool | None:
        reading = (self.coordinator.data or {}).get(f"{self.ac_number}_alarm")
        if reading is None or not reading.ok:
            return None
        return is_binary_active(reading.value)

    @callback
    def _handle_coordinator_update(self) -> None:
        faulted = self._read_faulted()
        if faulted is not None and faulted != self._faulted:
            was = self._faulted
            self._faulted = faulted
            if was is not None:
                code = self.coordinator.fault_code(self.ac_number)
                self._trigger_event(
                    EVENT_FAULT if faulted else EVENT_CLEARED,
                    {"fault_code": code} if code else {},
                )
                _LOGGER.info(
                    "%s %s%s",
                    self.point.object_name,
                    "faulted" if faulted else "cleared",
                    f" with code {code}" if code and faulted else "",
                )
        super()._handle_coordinator_update()
