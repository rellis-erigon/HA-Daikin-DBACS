"""Building a Lovelace card for a discovered indoor unit.

Home Assistant's own device page cannot host a custom card, so the supported
way to get a generated card onto a real page is a dashboard strategy. The
`custom:bridge-devices` strategy in HA-Cards asks each bridge what cards it
has and lays them out by area; this is the answer for this one.

Nothing here reaches into Home Assistant: given a group and what is known
about it, it returns a card. That makes the shape testable, which matters
because the chain from here to something visible is long — integration,
service, strategy, card, faceplate — and a mistake anywhere in it renders a
card that is simply dark.

Two deliberate choices in what comes out:

- **Both `device:` and `climate:` are set** when there is a thermostat. The
  card prefers an explicit per-role binding and falls back to the climate
  entity, so the roles a climate entity cannot carry — fault code, filter,
  compressor, comms — come from the device's own entities while mode and
  the temperatures come from either. Setting only `climate:` would lose the
  lamps; setting only `device:` would lose control.
- **No `entities:` block.** The shared role vocabulary binds a Daikin unit
  from the device alone, whichever bridge it arrived through. Writing out
  sixteen entity ids per unit would work and would then be wrong the moment
  somebody enabled a seventeenth point.
"""

from __future__ import annotations

from typing import Any

#: What a unit is drawn with unless the mapping says otherwise. The full
#: status faceplate rather than the tile: a generated dashboard is read one
#: unit at a time, and the tile exists for grids somebody lays out by hand.
DEFAULT_FACEPLATE = "indoor-unit"

CARD_TYPE = "custom:hvac-controller-card"


def card_for(
    *,
    device_name: str,
    faceplate: str | None = None,
    climate_entity: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """The card for one indoor-unit group."""
    card: dict[str, Any] = {
        "type": CARD_TYPE,
        "faceplate": faceplate or DEFAULT_FACEPLATE,
        "device": device_name,
    }
    if title:
        card["title"] = title
    if climate_entity:
        card["climate"] = climate_entity
    return card


def generated(
    *,
    device_name: str,
    area_id: str | None = None,
    faceplate: str | None = None,
    climate_entity: str | None = None,
) -> dict[str, Any]:
    """One entry in the shape the bridge-devices strategy consumes.

    The strategy groups by `area_id` and labels by `device`, so a unit with
    no area still appears — under "no area" rather than not at all, because
    a unit nobody has placed yet is still a unit somebody wants to see.
    """
    entry: dict[str, Any] = {
        "device": device_name,
        "card": card_for(
            device_name=device_name,
            faceplate=faceplate,
            climate_entity=climate_entity,
            title=device_name,
        ),
    }
    if area_id:
        entry["area_id"] = area_id
    return entry
