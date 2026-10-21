"""Rated capacity of a Daikin VRV indoor unit, read from its model number.

The gateway does not know what is on the end of each address; the site
mapping file can say which model it is, and a Daikin indoor model carries
its capacity class straight after the series letters: FXDQ40TV1B is the
40 class, FXMQ250PV1A the 250. Every VRV indoor series rates a class at the
same nominal duty, so the class alone gives the kW.

These are Daikin's nominal ratings (cooling at 27 °C DB / 19 °C WB inside
and 35 °C outside; heating at 20 °C inside and 7 °C outside). What a unit
actually delivers moves with the outdoor unit, pipe run and conditions, so
they are for sizing and comparison, not a live measurement.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class Capacity(NamedTuple):
    cooling_kw: float
    heating_kw: float


#: Capacity class -> nominal (cooling, heating) kW.
CLASSES: dict[int, Capacity] = {
    15: Capacity(1.7, 1.9),
    20: Capacity(2.2, 2.5),
    25: Capacity(2.8, 3.2),
    32: Capacity(3.6, 4.0),
    40: Capacity(4.5, 5.0),
    50: Capacity(5.6, 6.3),
    63: Capacity(7.1, 8.0),
    71: Capacity(8.0, 9.0),
    80: Capacity(9.0, 10.0),
    100: Capacity(11.2, 12.5),
    125: Capacity(14.0, 16.0),
    140: Capacity(16.0, 18.0),
    200: Capacity(22.4, 25.0),
    250: Capacity(28.0, 31.5),
}

#: An indoor model: FX, the series letters, then the class digits.
_INDOOR = re.compile(r"^\s*FX[A-Z]{1,3}(\d{2,3})", re.IGNORECASE)


def capacity_of(model: str | None) -> Capacity | None:
    """The nominal capacity of an indoor model, or None if it is not one
    whose class can be read."""
    if not model:
        return None
    match = _INDOOR.match(model)
    if match is None:
        return None
    return CLASSES.get(int(match.group(1)))
