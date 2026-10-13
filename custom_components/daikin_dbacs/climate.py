"""A climate entity per indoor-unit group.

This is the natural Home Assistant object for an air conditioner, and it is
also what makes the integration usable from a card without binding eight
separate points: a climate entity carries mode, action, both temperatures,
fan speed and swing on one entity, and writes through the same one.

Control is off until it is asked for. The entity is only created when it is
on, because a thermostat that refuses to act is worse than no thermostat.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DbacsConfigEntry
from .const import DEFAULT_MAX_TEMP, DEFAULT_MIN_TEMP, TEMP_STEP
from .entity import DbacsEntity
from .objects import MEMBERS_BY_KEY, PointRef, is_binary_active
from .writes import WriteRefused

_LOGGER = logging.getLogger(__name__)

#: Daikin's air-conditioning mode enumeration, both ways.
MODE_TO_HVAC: dict[int, HVACMode] = {
    1: HVACMode.COOL,
    2: HVACMode.HEAT,
    3: HVACMode.FAN_ONLY,
    4: HVACMode.AUTO,
    5: HVACMode.DRY,
}
HVAC_TO_MODE = {mode: value for value, mode in MODE_TO_HVAC.items()}

#: What the unit is doing, once we know it is on and the thermostat is calling.
ACTION_BY_MODE: dict[HVACMode, HVACAction] = {
    HVACMode.COOL: HVACAction.COOLING,
    HVACMode.HEAT: HVACAction.HEATING,
    HVACMode.DRY: HVACAction.DRYING,
    HVACMode.FAN_ONLY: HVACAction.FAN,
}

#: Daikin's airflow enumeration is Low, High, Middle, Auto — in that order.
#: Presented to Home Assistant in the order a person expects instead.
FAN_TO_VALUE: dict[str, int] = {
    "low": 1,
    "medium": 3,
    "high": 2,
    "auto": 4,
}
VALUE_TO_FAN = {value: name for name, value in FAN_TO_VALUE.items()}
FAN_ORDER = ["auto", "low", "medium", "high"]

#: Air direction is 0-3 horizontal, 4 vertical, 7 swing — not contiguous.
SWING_TO_VALUE: dict[str, int] = {
    "horizontal": 0,
    "vertical": 4,
    "both": 7,
}
SWING_ORDER = ["horizontal", "vertical", "both"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DbacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One climate entity per live group, when control is enabled."""
    coordinator = entry.runtime_data
    if not coordinator.control_enabled:
        _LOGGER.debug(
            "control is off for %s, so no climate entities are created",
            coordinator.gateway.host,
        )
        return

    entities = []
    for ac_number in coordinator.active_groups:
        group = coordinator.discovery.groups.get(ac_number)
        # Without a power command and a mode command there is nothing to
        # control, and a climate entity would be a decoration.
        if group is not None and not (
            group.publishes(MEMBERS_BY_KEY["on_off_command"].number)
            and group.publishes(MEMBERS_BY_KEY["mode_command"].number)
        ):
            continue
        entities.append(DbacsClimate(coordinator, ac_number))
    async_add_entities(entities)


class DbacsClimate(DbacsEntity, ClimateEntity):
    """One indoor-unit group, as a thermostat."""

    _attr_name = None  # the device name is the entity name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = TEMP_STEP
    _attr_min_temp = DEFAULT_MIN_TEMP
    _attr_max_temp = DEFAULT_MAX_TEMP

    def __init__(self, coordinator, ac_number: int) -> None:
        # The climate entity stands for the whole group rather than one
        # point, so it borrows the power status member for identity.
        super().__init__(
            coordinator, ac_number, MEMBERS_BY_KEY["on_off_status"]
        )
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{ac_number}_climate"
        )

        group = coordinator.discovery.groups.get(ac_number)

        def has(key: str) -> bool:
            member = MEMBERS_BY_KEY[key]
            return group is None or group.publishes(member.number)

        self._has_fan = has("fan_command")
        self._has_swing = has("air_direction_command")
        self._has_setpoint = has("setpoint")

        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self._has_setpoint:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._has_fan:
            features |= ClimateEntityFeature.FAN_MODE
            self._attr_fan_modes = FAN_ORDER
        if self._has_swing:
            features |= ClimateEntityFeature.SWING_MODE
            self._attr_swing_modes = SWING_ORDER
        self._attr_supported_features = features

        self._attr_hvac_modes = [HVACMode.OFF] + [
            MODE_TO_HVAC[value] for value in sorted(MODE_TO_HVAC)
        ]

    # --- reading -------------------------------------------------------------

    def _value(self, key: str) -> Any:
        reading = (self.coordinator.data or {}).get(f"{self.ac_number}_{key}")
        if reading is None or not reading.ok:
            return None
        return reading.value

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        group = self.coordinator.discovery.groups.get(self.ac_number)
        if group is not None and not group.live:
            return False
        # Powered state is the one reading this entity cannot do without.
        return self._value("on_off_status") is not None

    @property
    def _powered(self) -> bool | None:
        return is_binary_active(self._value("on_off_status"))

    @property
    def hvac_mode(self) -> HVACMode | None:
        if self._powered is False:
            return HVACMode.OFF
        raw = self._value("mode_status")
        if raw is None:
            return None
        try:
            return MODE_TO_HVAC.get(int(raw))
        except (TypeError, ValueError):
            return None

    @property
    def hvac_action(self) -> HVACAction | None:
        if self._powered is False:
            return HVACAction.OFF
        if self._powered is None:
            return None

        # The thermostat says whether the unit is calling for anything at
        # all. Without it the compressor is the next best witness, and with
        # neither we only know the mode it is set to.
        thermostat = is_binary_active(self._value("thermo_status"))
        compressor = is_binary_active(self._value("compressor_status"))
        calling = thermostat if thermostat is not None else compressor

        mode = self.hvac_mode
        if mode in (None, HVACMode.OFF):
            return None
        if mode is HVACMode.FAN_ONLY:
            return HVACAction.FAN
        if calling is False:
            return HVACAction.IDLE
        if mode is HVACMode.AUTO:
            # The status object never reports Auto, so this only happens if a
            # gateway surprises us. Idle is the honest answer.
            return HVACAction.IDLE
        return ACTION_BY_MODE.get(mode, HVACAction.IDLE)

    @property
    def current_temperature(self) -> float | None:
        raw = self._value("room_temp")
        try:
            return round(float(raw), 1)
        except (TypeError, ValueError):
            return None

    @property
    def target_temperature(self) -> float | None:
        raw = self._value("setpoint")
        try:
            return round(float(raw), 1)
        except (TypeError, ValueError):
            return None

    @property
    def fan_mode(self) -> str | None:
        raw = self._value("fan_status")
        try:
            return VALUE_TO_FAN.get(int(raw))
        except (TypeError, ValueError):
            return None

    @property
    def swing_mode(self) -> str | None:
        raw = self._value("air_direction_status")
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            return None
        if value == 7:
            return "both"
        if value == 4:
            return "vertical"
        if 0 <= value <= 3:
            return "horizontal"
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            "diii_address": self.coordinator.discovery.groups[
                self.ac_number
            ].label
            if self.ac_number in self.coordinator.discovery.groups
            else None,
            "bacnet_write_priority": self.coordinator.write_priority,
        }
        code = self.coordinator.fault_code(self.ac_number)
        if code:
            attributes["fault_code"] = code
        filter_sign = is_binary_active(self._value("filter_sign"))
        if filter_sign is not None:
            attributes["filter_dirty"] = filter_sign
        return attributes

    # --- writing -------------------------------------------------------------

    async def _write(self, key: str, value: Any) -> None:
        point = PointRef(self.ac_number, MEMBERS_BY_KEY[key])
        try:
            await self.coordinator.async_write(point, value)
        except WriteRefused as err:
            raise HomeAssistantError(str(err)) from err

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode is HVACMode.OFF:
            await self._write("on_off_command", "inactive")
            return
        value = HVAC_TO_MODE.get(hvac_mode)
        if value is None:
            raise HomeAssistantError(f"{hvac_mode} is not a mode this unit has")
        # Mode first, then power: a unit told to run before it is told what
        # to do runs in whatever mode it was last left in, which is visible
        # in the room.
        await self._write("mode_command", value)
        if self._powered is not True:
            await self._write("on_off_command", "active")

    async def async_turn_on(self) -> None:
        await self._write("on_off_command", "active")

    async def async_turn_off(self) -> None:
        await self._write("on_off_command", "inactive")

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        # The unit clamps silently to its own limits, which depend on the
        # indoor and outdoor models and the current mode, so saying what was
        # asked for is the most we can honestly do.
        await self._write("setpoint", float(temperature))

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        value = FAN_TO_VALUE.get(fan_mode)
        if value is None:
            raise HomeAssistantError(f"{fan_mode} is not a fan speed this unit has")
        await self._write("fan_command", value)

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        value = SWING_TO_VALUE.get(swing_mode)
        if value is None:
            raise HomeAssistantError(
                f"{swing_mode} is not an air direction this unit has"
            )
        await self._write("air_direction_command", float(value))
