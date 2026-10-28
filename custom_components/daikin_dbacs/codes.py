"""What a Daikin two-character malfunction code means.

The gateway gives the code (`E5`, `U4`) and nothing else. These are the
standard VRV service-manual meanings, as a starting point for whoever goes
to the unit: the same code can have more than one cause, and the service
manual for the actual model is the authority. A code not listed here is
shown as the code alone, never guessed at.

The first character says where to look: A indoor unit, C indoor sensors,
E/F/H/J/L/P outdoor unit, U system and communication.
"""

from __future__ import annotations

MEANINGS: dict[str, str] = {
    # Indoor unit
    "A0": "external protection device activated",
    "A1": "indoor unit PCB fault",
    "A3": "drain level fault (drain pump or float switch)",
    "A6": "indoor fan motor locked or overloaded",
    "A7": "swing flap motor fault",
    "A9": "indoor expansion valve fault",
    "AF": "drain or humidifier fault",
    "AJ": "indoor capacity setting fault",
    # Indoor sensors
    "C4": "indoor coil liquid-pipe thermistor fault",
    "C5": "indoor coil gas-pipe thermistor fault",
    "C9": "indoor return-air thermistor fault",
    "CJ": "wall controller thermistor fault",
    # Outdoor unit
    "E1": "outdoor unit PCB fault",
    "E3": "high-pressure switch tripped",
    "E4": "low-pressure fault",
    "E5": "inverter compressor locked or overloaded",
    "E6": "compressor overcurrent",
    "E7": "outdoor fan motor fault",
    "E9": "outdoor expansion valve fault",
    "F3": "compressor discharge temperature too high",
    "F6": "refrigerant overcharge or high pressure",
    "H3": "high-pressure switch fault",
    "H7": "outdoor fan motor signal fault",
    "H9": "outdoor air thermistor fault",
    "J3": "discharge pipe thermistor fault",
    "J5": "suction pipe thermistor fault",
    "J6": "outdoor coil thermistor fault",
    "J9": "subcooling heat-exchanger thermistor fault",
    "JA": "high-pressure sensor fault",
    "JC": "low-pressure sensor fault",
    "L4": "inverter heat sink too hot",
    "L5": "inverter compressor instantaneous overcurrent",
    "L8": "inverter compressor overcurrent",
    "L9": "inverter compressor failed to start",
    "LC": "outdoor inverter and control PCB not communicating",
    "P1": "power supply imbalance or lost phase",
    "P4": "inverter heat-sink thermistor fault",
    "PJ": "outdoor capacity setting fault",
    # System and communication
    "U0": "low refrigerant",
    "U1": "power supply reversed or lost phase",
    "U2": "power supply voltage fault",
    "U3": "commissioning test run not completed",
    "U4": "indoor and outdoor units not communicating",
    "U5": "indoor unit and wall controller not communicating",
    "U7": "outdoor units not communicating with each other",
    "U8": "main and sub wall controllers not communicating",
    "U9": "another unit on the same system has a fault",
    "UA": "unit combination or connection mismatch",
    "UC": "duplicate central control address",
    "UE": "central controller and indoor unit not communicating",
    "UF": "refrigerant piping and wiring mismatch",
    "UH": "auto-addressing fault",
}


def meaning(code: str | None) -> str | None:
    """The standard meaning of a code, or None if it is not a known one."""
    if not code:
        return None
    return MEANINGS.get(code.strip().upper()[:2])


def describe(code: str | None) -> str | None:
    """`E5 (inverter compressor locked or overloaded)`, or the code alone."""
    if not code:
        return None
    what = meaning(code)
    return f"{code} ({what})" if what else code
