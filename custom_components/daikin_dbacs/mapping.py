"""Applying a site's own names and areas to the discovered units.

A gateway knows a unit by its DIII-NET address and nothing else: no room
name, no equipment tag, no area. Everything that makes 96 identical devices
legible comes from outside — a commissioning report, a mechanical schedule,
a spreadsheet somebody keeps.

So the integration reads a mapping file at runtime and never ships one. The
file is site data; this module is the part that can be reasoned about and
tested.

Deliberately conservative, in two ways that matter on a building somebody
else also works on:

- **A choice already made wins.** A device that has been renamed or placed
  by hand is left exactly as it is, because the person who did that knew
  something the file does not.
- **It reports before it acts.** The plan is computed first and returned, so
  a dry run is the same code path as the real thing rather than a separate
  description of it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .objects import MAX_AC_NUMBER, address_label

_LOGGER = logging.getLogger(__name__)

_DIGITS = re.compile(r"\d+")

#: Where mapping files are looked for. Outside the configuration directory
#: on purpose: it is site data shared with the other bridges, and it must
#: never be mistaken for part of this integration.
SITE_DIR = "/share/HA-Daikin-DBACS-site"


def path_for(device_instance: int | str) -> str:
    """The mapping file for one gateway.

    A DIII-NET address is only unique within a gateway — both gateways on a
    site number their units from the same formula, so `1 · 1-00` exists on
    each and means a different room on each. One file per gateway, named
    after its BACnet device instance, is what keeps them apart.

    Refusing a missing or non-numeric instance matters more than it looks:
    falling back to some other gateway's file would name every unit
    plausibly and wrongly.
    """
    try:
        instance = int(device_instance)
    except (TypeError, ValueError) as err:
        raise MappingError(
            "the gateway's BACnet device instance is not known, so its "
            "mapping file cannot be identified"
        ) from err
    if instance < 0:
        raise MappingError(
            f"{instance} is not a BACnet device instance"
        )
    return f"{SITE_DIR}/gateway-{instance}-units.json"


class MappingError(Exception):
    """The mapping file could not be used, and why."""


@dataclass(frozen=True, slots=True)
class UnitMapping:
    """What a site says about one indoor-unit group."""

    ac_number: int
    name: str | None = None
    area: str | None = None
    tag: str | None = None
    model: str | None = None
    system: str | None = None
    outdoor: str | None = None
    faceplate: str | None = None
    """Which card face this unit is drawn with, where a site has a
    preference. Left out, the generated card uses the default."""

    @property
    def label(self) -> str:
        return address_label(self.ac_number)


@dataclass(slots=True)
class Change:
    """One thing that would be done, or was."""

    ac_number: int
    label: str
    field: str
    old: str | None
    new: str
    applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "address": self.label,
            "change": self.field,
            "from": self.old,
            "to": self.new,
            "applied": self.applied,
        }


@dataclass(slots=True)
class Plan:
    """Everything a mapping would do, and everything it would not."""

    changes: list[Change] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    """Devices left alone because somebody had already named or placed them."""
    unmapped: list[str] = field(default_factory=list)
    """Live groups the file says nothing about."""
    unknown: list[str] = field(default_factory=list)
    """Entries in the file with no matching live group."""
    areas_to_create: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "changes": [c.as_dict() for c in self.changes],
            "change_count": len(self.changes),
            "applied_count": sum(1 for c in self.changes if c.applied),
            "left_alone": self.kept,
            "not_in_the_file": self.unmapped,
            "in_the_file_but_not_on_the_gateway": self.unknown,
            "areas_to_create": self.areas_to_create,
        }


def load(path: str) -> dict[int, UnitMapping]:
    """Read one gateway's mapping file, keyed by DIII address or AC number.

    Accepts the address as the integration writes it (``"1 · 2-14"``), as
    plain text (``"1 2-14"`` or ``"1-2-14"``), or an ``ac_number`` field.
    Being forgiving here is worth it: the file is maintained by hand, and
    rejecting it over a middle dot would be absurd.

    The path is required rather than defaulted. An address is only unique
    within a gateway, so a default would be a way of silently reading some
    other gateway's rooms. Use `path_for` to name the file.
    """
    target = Path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise MappingError(
            f"no mapping file at {target}. It holds this site's room names "
            "and areas, and is not shipped with the integration."
        ) from err
    except OSError as err:
        raise MappingError(f"could not read {target}: {err}") from err
    except json.JSONDecodeError as err:
        raise MappingError(f"{target} is not valid JSON: {err}") from err

    if not isinstance(raw, dict):
        raise MappingError(
            f"{target} should be an object keyed by DIII address, not a "
            f"{type(raw).__name__}"
        )

    out: dict[int, UnitMapping] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        ac = _ac_number_of(key, value)
        if ac is None:
            _LOGGER.warning("mapping: cannot read %r as a DIII address", key)
            continue
        out[ac] = UnitMapping(
            ac_number=ac,
            name=_text(value.get("name")),
            area=_text(value.get("area")),
            tag=_text(value.get("tag") or value.get("unit")),
            model=_text(value.get("model")),
            system=_text(value.get("system")),
            outdoor=_text(value.get("outdoor")),
            faceplate=_text(value.get("faceplate")),
        )
    if not out:
        raise MappingError(f"{target} held no usable entries")
    return out


def plan(
    mapping: dict[int, UnitMapping],
    devices: dict[int, Any],
    *,
    set_names: bool = True,
    set_areas: bool = True,
    existing_areas: dict[str, str] | None = None,
    overwrite: bool = False,
) -> Plan:
    """Work out what applying a mapping would change.

    `devices` is the live groups, keyed by AC number, each with
    `name_by_user` and `area_id`. `existing_areas` maps a lowercased area
    name to its id.
    """
    areas = {k.strip().lower(): v for k, v in (existing_areas or {}).items()}
    result = Plan()

    for ac in sorted(devices):
        entry = mapping.get(ac)
        if entry is None:
            result.unmapped.append(address_label(ac))
            continue

        device = devices[ac]
        named = _text(getattr(device, "name_by_user", None))
        placed = _text(getattr(device, "area_id", None))

        if set_names and entry.name:
            if named and not overwrite:
                result.kept.append(f"{address_label(ac)} is already {named!r}")
            elif named != entry.name:
                result.changes.append(
                    Change(ac, address_label(ac), "name", named, entry.name)
                )

        if set_areas and entry.area:
            wanted_id = areas.get(entry.area.strip().lower())
            if placed and not overwrite:
                result.kept.append(
                    f"{address_label(ac)} is already in an area"
                )
            elif wanted_id is None:
                if entry.area not in result.areas_to_create:
                    result.areas_to_create.append(entry.area)
                result.changes.append(
                    Change(ac, address_label(ac), "area", placed, entry.area)
                )
            elif placed != wanted_id:
                result.changes.append(
                    Change(ac, address_label(ac), "area", placed, entry.area)
                )

    for ac in sorted(mapping):
        if ac not in devices:
            result.unknown.append(address_label(ac))

    return result


# --- helpers -----------------------------------------------------------------


def _ac_number_of(key: str, value: dict[str, Any]) -> int | None:
    """The AC number a mapping entry refers to."""
    explicit = value.get("ac_number")
    if isinstance(explicit, int) and 0 <= explicit <= MAX_AC_NUMBER:
        return explicit

    digits = [int(part) for part in _DIGITS.findall(str(key))]
    if len(digits) != 3:
        return None
    port, line, group = digits
    if not (1 <= port <= 4 and 1 <= line <= 4 and 0 <= group <= 15):
        return None
    return (port - 1) * 64 + (line - 1) * 16 + group


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
