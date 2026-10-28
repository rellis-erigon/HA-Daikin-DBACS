"""Refrigerant systems, rolled up from the indoor units on them.

The gateway publishes no outdoor-unit objects at all. The only outdoor
information it carries is `CompressorStatus` on each indoor unit — "the
compressor serving *me* is running" — so an outdoor unit can only be
described by agreeing with everything downstream of it.

Which units share a system is not derivable either: it comes from the site
mapping file. No mapping, no outdoor devices, which is the honest outcome
rather than a guess.

The roll-up exists because of how an outdoor fault actually presents. On the
system this was built against, one condenser went down and three of its
eight indoor units reported a malfunction code — the three that happened to
be calling. The other five read Normal, because a unit that is not calling
cannot see the fault. Read unit by unit that is three unrelated faults;
read as a system it is one condenser with its compressor off while rooms
ask for heat.

That last condition is the one worth an entity of its own, and it is the
whole point of this module: **nothing is calling for anything and getting
it.**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .objects import address_label, is_binary_active


@dataclass(frozen=True, slots=True)
class System:
    """One refrigerant system: an outdoor unit and the units it serves."""

    name: str
    outdoor: str | None
    ac_numbers: tuple[int, ...]
    area: str | None = None
    """Where the site mapping says the outdoor unit is (`outdoor_area`)."""

    @property
    def label(self) -> str:
        return self.outdoor or self.name

    @property
    def addresses(self) -> tuple[str, ...]:
        return tuple(address_label(ac) for ac in self.ac_numbers)


@dataclass(slots=True)
class Rollup:
    """What the indoor units say about their system, taken together."""

    system: System
    indoor_total: int = 0
    answering: int = 0
    running: int = 0
    calling: int = 0
    compressor_on: int = 0
    needing_compressor: int = 0
    """Units running in a mode that cannot work without the compressor."""
    in_alarm: int = 0
    codes: list[str] = field(default_factory=list)
    alarm_addresses: list[str] = field(default_factory=list)

    @property
    def compressor_running(self) -> bool | None:
        """Whether the outdoor unit's compressor is running at all.

        Any indoor unit seeing it is enough: they are all reporting the
        same compressor.
        """
        if not self.answering:
            return None
        return self.compressor_on > 0

    @property
    def faulted(self) -> bool | None:
        if not self.answering:
            return None
        return self.in_alarm > 0

    @property
    def not_delivering(self) -> bool | None:
        """Something is calling for heating or cooling and getting none.

        A VRV outdoor unit cycles its compressor with demand, so a stopped
        compressor is normal and says nothing on its own. Measured across
        fourteen systems on a live site, every one with its compressor
        running had at least one unit calling, and every one with it stopped
        had none — bar the single case this entity is for.

        An earlier version of this keyed on units *running* rather than
        calling, on the back of a misread fault. It fired on six systems out
        of fourteen, which is noise rather than a detector: a unit switched
        on and already at its setpoint is supposed to have no compressor
        behind it.

        **Use this with a duration.** A compressor takes time to start, and
        a unit that has just begun calling will show this true for a minute
        or two quite legitimately. In an automation, give it a `for:` of a
        few minutes.
        """
        if not self.answering:
            return None
        if not self.calling:
            return False
        return self.compressor_on == 0

    @property
    def fault_code(self) -> str | None:
        """The code the units on this system agree on, where they do."""
        if not self.codes:
            return None
        first = self.codes[0]
        if all(code == first for code in self.codes):
            return first
        return ", ".join(sorted(set(self.codes)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "system": self.system.name,
            "outdoor_unit": self.system.outdoor,
            "indoor_units": self.indoor_total,
            "answering": self.answering,
            "running": self.running,
            "calling": self.calling,
            "running_needing_compressor": self.needing_compressor,
            "compressor_running": self.compressor_running,
            "in_alarm": self.in_alarm,
            "fault_code": self.fault_code,
            "units_in_alarm": self.alarm_addresses,
            "not_delivering": self.not_delivering,
        }


def systems_from_mapping(mapping: dict[int, Any]) -> dict[str, System]:
    """Group the mapped units by the refrigerant system they are on."""
    grouped: dict[str, list[int]] = {}
    outdoors: dict[str, str | None] = {}
    areas: dict[str, str | None] = {}
    for ac, entry in sorted(mapping.items()):
        name = getattr(entry, "system", None)
        if not name:
            continue
        grouped.setdefault(name, []).append(ac)
        outdoors.setdefault(name, getattr(entry, "outdoor", None))
        if not areas.get(name):
            areas[name] = getattr(entry, "outdoor_area", None)
    return {
        name: System(name=name, outdoor=outdoors.get(name),
                     ac_numbers=tuple(acs), area=areas.get(name))
        for name, acs in sorted(grouped.items())
    }


def roll_up(
    system: System,
    readings: dict[str, Any],
    live: set[int],
    fault_codes: dict[int, str | None] | None = None,
) -> Rollup:
    """What one system's indoor units add up to.

    Only units the gateway is actually publishing count towards the totals.
    A group in the mapping file that the gateway does not have is somebody
    else's problem — probably a decommissioned unit — and counting it would
    make a healthy system look half dead.
    """
    codes = fault_codes or {}
    present = tuple(ac for ac in system.ac_numbers if ac in live)
    out = Rollup(system=system, indoor_total=len(present))

    for ac in present:
        powered = _flag(readings, ac, "on_off_status")
        if powered is None:
            continue
        out.answering += 1
        if powered:
            out.running += 1
            if _needs_compressor(readings, ac):
                out.needing_compressor += 1
        if _flag(readings, ac, "thermo_status"):
            out.calling += 1
        if _flag(readings, ac, "compressor_status"):
            out.compressor_on += 1
        if _flag(readings, ac, "alarm"):
            out.in_alarm += 1
            out.alarm_addresses.append(address_label(ac))
            code = codes.get(ac)
            if code:
                out.codes.append(code)
    return out


#: Daikin's air-conditioning mode enumeration. Fan-only is mode 3 and is
#: the one that does not need the outdoor unit.
_FAN_ONLY = 3


def _needs_compressor(readings: dict[str, Any], ac: int) -> bool:
    """Whether this unit's mode requires the compressor.

    An unreadable mode counts as needing it: a unit that is switched on and
    will not say what it is doing is more likely to be cooling than to be
    blowing air about, and the cost of being wrong is a sensor that reads
    `on` for a few minutes rather than a fault nobody saw.
    """
    reading = readings.get(f"{ac}_mode_status")
    if reading is None or not getattr(reading, "ok", False):
        return True
    try:
        return int(reading.value) != _FAN_ONLY
    except (TypeError, ValueError):
        return True


def _flag(readings: dict[str, Any], ac: int, key: str) -> bool | None:
    reading = readings.get(f"{ac}_{key}")
    if reading is None or not getattr(reading, "ok", False):
        return None
    return is_binary_active(reading.value)
