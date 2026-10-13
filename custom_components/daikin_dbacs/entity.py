"""The base entity: one point of one indoor-unit group."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import DbacsCoordinator
from .gateway import PointValue
from .objects import Member, PointRef, address_label, diii_address


class DbacsEntity(CoordinatorEntity[DbacsCoordinator]):
    """A point on a group, tied to the group's Home Assistant device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DbacsCoordinator,
        ac_number: int,
        member: Member,
    ) -> None:
        super().__init__(coordinator)
        self.ac_number = ac_number
        self.member = member
        self.point = PointRef(ac_number, member)

        entry_id = coordinator.entry.entry_id
        port, line, group = diii_address(ac_number)

        self._attr_unique_id = f"{entry_id}_{ac_number}_{member.key}"
        self._attr_name = member.label
        self._attr_entity_registry_enabled_default = (
            member.key in coordinator.enabled_member_keys
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{ac_number}")},
            name=f"DBACS {address_label(ac_number)}",
            manufacturer=MANUFACTURER,
            model=MODEL,
            serial_number=f"D3 {port}-{line}-{group:02d}",
        )
        # Hang the group off the gateway's own device, by registry id rather
        # than by identifier: `via_device` is deprecated, and it silently
        # tolerated pointing at a device that had never been created.
        if coordinator.hub_device_id is not None:
            self._attr_device_info["via_device_id"] = coordinator.hub_device_id

    @property
    def reading(self) -> PointValue | None:
        """This point's last reading, whatever its state."""
        return (self.coordinator.data or {}).get(self.point.key)

    @property
    def available(self) -> bool:
        """Available only when we have an actual answer for this point.

        A point that errored, or that the gateway left out of a response,
        reads unavailable rather than keeping its last value. A confident
        number from a dead point is the one failure worth designing against.
        """
        if not self.coordinator.last_update_success:
            return False
        group = self.coordinator.discovery.groups.get(self.ac_number)
        if group is not None and not group.live:
            return False
        reading = self.reading
        return reading is not None and reading.ok

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        reading = self.reading
        attributes: dict[str, str] = {
            "bacnet_object": self.point.object_name,
            "bacnet_instance": str(self.point.instance),
            "diii_address": address_label(self.ac_number),
        }
        if reading is not None and reading.error:
            attributes["error"] = reading.error
        if self.member.note:
            attributes["note"] = self.member.note
        return attributes
