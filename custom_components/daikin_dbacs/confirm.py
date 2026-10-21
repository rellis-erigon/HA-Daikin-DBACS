"""Has a write taken? Matching each command to what the unit reports back.

A write the gateway accepts is only a request: the unit may refuse it, clamp
it, or never hear it if DIII-NET is busy. What it is actually doing arrives
on a separate status object — mode on AirConModeStatus, not on the command —
so a change counts as confirmed only when that status, read fresh from the
gateway after the write went out, agrees with what was asked for.

The setpoint is the awkward one: it is a single read/write object, and the
integration shows a written value at once so the control does not spring
back. So a setpoint is confirmed only by a reading that left the gateway
after the write, never by the value we put there ourselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .objects import PointRef, is_binary_active

#: Command member -> (status member that confirms it, setting name).
#: The setting names are Home Assistant's own climate attribute names, so a
#: card can match them to what it changed without knowing about BACnet.
CONFIRMED_BY: dict[str, tuple[str, str]] = {
    "on_off_command": ("on_off_status", "power"),
    "mode_command": ("mode_status", "hvac_mode"),
    "fan_command": ("fan_status", "fan_mode"),
    "setpoint": ("setpoint", "temperature"),
    "air_direction_command": ("air_direction_status", "swing_mode"),
}

#: Daikin's Auto mode value. The status object never reports Auto: a unit
#: in Auto reports the Cool or Heat it has chosen.
_AUTO = 4


@dataclass(slots=True)
class Awaiting:
    """A write not yet seen to take."""

    point: PointRef
    value: Any
    requested_at: float
    sent_at: float | None = None
    """None while the write is held back by the rate guard."""

    @property
    def setting(self) -> str:
        return CONFIRMED_BY[self.point.member.key][1]

    @property
    def status_member(self) -> str:
        return CONFIRMED_BY[self.point.member.key][0]

    @property
    def status_key(self) -> str:
        return f"{self.point.ac_number}_{CONFIRMED_BY[self.point.member.key][0]}"


def matches(command: str, wanted: Any, reported: Any) -> bool:
    """Whether the status `reported` shows the command `wanted` took."""
    if reported is None:
        return False
    if command == "on_off_command":
        return is_binary_active(reported) == is_binary_active(wanted)
    if command == "setpoint":
        try:
            return abs(float(reported) - float(wanted)) < 0.05
        except (TypeError, ValueError):
            return False
    try:
        want = int(float(wanted))
        got = int(float(reported))
    except (TypeError, ValueError):
        return False
    if command == "mode_command" and want == _AUTO:
        return got in (1, 2, _AUTO)
    return want == got
