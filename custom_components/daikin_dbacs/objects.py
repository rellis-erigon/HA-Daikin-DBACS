"""The Daikin D-BACS object model.

Everything in this module is pure: no Home Assistant, no BACnet stack, no
network. It is the part of the integration that can be tested exhaustively,
and it is where the gateway's quirks are written down once instead of being
rediscovered in a platform module.

The gateway packs its object instance numbers like this::

    bit  31────22 21────16 15──────8 7──────0
         obj type  unused   AC number  member

so for the instance portion alone::

    instance = (ac_number * 256) + member

The AC number is a position in DIII-NET address order, four ports of four
lines of sixteen groups, which is how a published instance number can be
turned back into the address written on the wall controller.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

# --- instance number encoding ------------------------------------------------

MEMBERS_PER_UNIT: Final = 256
PORTS: Final = 4
LINES_PER_PORT: Final = 4
GROUPS_PER_LINE: Final = 16
GROUPS_PER_PORT: Final = LINES_PER_PORT * GROUPS_PER_LINE  # 64
MAX_AC_NUMBER: Final = PORTS * GROUPS_PER_PORT - 1  # 255


def instance_of(ac_number: int, member: int) -> int:
    """The BACnet instance number for a member of one indoor-unit group."""
    if not 0 <= ac_number <= MAX_AC_NUMBER:
        raise ValueError(f"ac_number out of range: {ac_number}")
    if not 0 <= member < MEMBERS_PER_UNIT:
        raise ValueError(f"member out of range: {member}")
    return ac_number * MEMBERS_PER_UNIT + member


def split_instance(instance: int) -> tuple[int, int]:
    """Split an instance number back into (ac_number, member)."""
    return divmod(instance, MEMBERS_PER_UNIT)


def diii_address(ac_number: int) -> tuple[int, int, int]:
    """The DIII-NET (port, line, group) an AC number sits at.

    Port and line are 1-based and group is 0-based, matching the way Daikin
    writes them: port 2, line 1, group 5 is "2 · 1-05".
    """
    if not 0 <= ac_number <= MAX_AC_NUMBER:
        raise ValueError(f"ac_number out of range: {ac_number}")
    port, within = divmod(ac_number, GROUPS_PER_PORT)
    line, group = divmod(within, GROUPS_PER_LINE)
    return port + 1, line + 1, group


def ac_number_of(port: int, line: int, group: int) -> int:
    """The AC number at a DIII-NET address. The inverse of diii_address."""
    if not 1 <= port <= PORTS:
        raise ValueError(f"port out of range: {port}")
    if not 1 <= line <= LINES_PER_PORT:
        raise ValueError(f"line out of range: {line}")
    if not 0 <= group < GROUPS_PER_LINE:
        raise ValueError(f"group out of range: {group}")
    return (port - 1) * GROUPS_PER_PORT + (line - 1) * GROUPS_PER_LINE + group


def address_label(ac_number: int) -> str:
    """How the address is written on paper: "2 · 1-05"."""
    port, line, group = diii_address(ac_number)
    return f"{port} · {line}-{group:02d}"


# --- the member table --------------------------------------------------------


class Kind(StrEnum):
    """What a member is for, which decides whether we read it or write it."""

    STATUS = "status"  # readable state
    COMMAND = "command"  # written to change the unit
    RESET = "reset"  # written to clear something, has no meaningful state


class ObjType(StrEnum):
    """BACnet object types, spelled as bacpypes3 spells them.

    The numeric values Daikin documents are the standard ones, so these names
    are all the mapping needed.
    """

    AI = "analog-input"
    AV = "analog-value"
    BI = "binary-input"
    BO = "binary-output"
    BV = "binary-value"
    MI = "multi-state-input"
    MO = "multi-state-output"
    ACCUMULATOR = "accumulator"


@dataclass(frozen=True, slots=True)
class Member:
    """One of the points Daikin publishes per indoor-unit group."""

    number: int
    key: str
    """Stable identifier used in unique ids and in the options flow."""
    object_name: str
    """Daikin's own object name, minus the _XXX group suffix."""
    object_type: ObjType
    label: str
    kind: Kind = Kind.STATUS
    states: tuple[str | None, ...] = ()
    """Present_Value enumeration, indexed from 1. None means "documented but
    not used" — Daikin leaves a hole at mode 4."""
    unit: str | None = None
    cov: bool = False
    """Worth a change-of-value subscription rather than only a poll."""
    default_on: bool = False
    """Enabled by default, so a new gateway comes up looking complete."""
    gated: bool = False
    """Writing this affects people, not just numbers — it stays behind an
    explicit opt-in even once the write path exists."""
    note: str = ""


_MEMBERS: Final[tuple[Member, ...]] = (
    Member(
        1, "on_off_command", "StartStopCommand", ObjType.BO,
        "Power", Kind.COMMAND, states=("Stop", "Operation"),
    ),
    Member(
        2, "on_off_status", "StartStopStatus", ObjType.BI,
        "Running", states=("Stop", "Operation"), cov=True, default_on=True,
    ),
    Member(
        3, "alarm", "Alarm", ObjType.BI,
        "Alarm", states=("Normal", "Malfunction"), cov=True, default_on=True,
    ),
    Member(
        4, "malfunction_code", "MalfunctionCode", ObjType.MI,
        "Malfunction code", cov=True, default_on=True,
        note="Value 1 is normal; the two-character Daikin code is in the "
             "object's Description property, not its value.",
    ),
    Member(
        5, "mode_command", "AirConModeCommand", ObjType.MO,
        "Mode setting", Kind.COMMAND,
        states=("Cool", "Heat", "Ventilate", "Auto", "Dry"),
    ),
    Member(
        6, "mode_status", "AirConModeStatus", ObjType.MI,
        "Mode", states=("Cool", "Heat", "Fan", None, "Dry"), default_on=True,
        note="State 4 is documented but unused — the status object never "
             "reports Auto, it reports what the unit settled on.",
    ),
    Member(
        7, "fan_command", "AirFlowRateCommand", ObjType.MO,
        "Fan speed setting", Kind.COMMAND,
        states=("Low", "High", "Middle", "Auto"),
    ),
    Member(
        8, "fan_status", "AirFlowRateStatus", ObjType.MI,
        "Fan speed", states=("Low", "High", "Middle", "Auto"), default_on=True,
        note="The enumeration really is Low, High, Middle, Auto. It is not "
             "low-medium-high and assuming it is mislabels every unit.",
    ),
    Member(
        9, "room_temp", "RoomTemp", ObjType.AI,
        "Room temperature", unit="temperature", cov=True, default_on=True,
    ),
    Member(
        10, "setpoint", "TempAdjust", ObjType.AV,
        "Setpoint", unit="temperature", default_on=True,
        note="Range depends on the indoor and outdoor unit models and on the "
             "current mode. Out-of-range writes are clamped by the unit, "
             "silently.",
    ),
    Member(
        11, "filter_sign", "FilterSign", ObjType.BI,
        "Filter", states=("No", "Yes"), default_on=True,
    ),
    Member(
        12, "filter_sign_reset", "FilterSignReset", ObjType.BV,
        "Reset filter sign", Kind.RESET,
    ),
    Member(
        13, "remote_lock_on_off", "RemoteControlStart", ObjType.BV,
        "Wall controller: power", Kind.COMMAND,
        states=("Enabled", "Disabled"), gated=True,
    ),
    Member(
        14, "remote_lock_mode", "RemoteControlAirConModeSet", ObjType.BV,
        "Wall controller: mode", Kind.COMMAND,
        states=("Enabled", "Disabled"), gated=True,
    ),
    # Member 15 is vacant.
    Member(
        16, "remote_lock_setpoint", "RemoteControlTempAdjust", ObjType.BV,
        "Wall controller: setpoint", Kind.COMMAND,
        states=("Enabled", "Disabled"), gated=True,
    ),
    Member(
        17, "central_control_rejection", "CL_Rejection", ObjType.BV,
        "Reject sub-group control", Kind.COMMAND, gated=True,
    ),
    # Member 18 exists but is Japanese market only.
    Member(
        19, "accumulated_power", "AccumulatedPower", ObjType.ACCUMULATOR,
        "Accumulated power", unit="energy",
        note="Supported by certain indoor models only, and the one object "
             "the gateway will not send change-of-value for.",
    ),
    Member(
        20, "communication_status", "CommunicationStatus", ObjType.BI,
        "Communication", states=("Normal", "Fault"), default_on=True,
        note="Drives availability. A group whose communication is faulted is "
             "wired but not answering, which is not the same as absent.",
    ),
    Member(
        21, "system_forced_off", "SystemForcedOff", ObjType.BV,
        "Forced stop", Kind.COMMAND,
        states=("Clearance", "Forced stop"), gated=True,
    ),
    Member(
        22, "air_direction_command", "AirDirectionCommand", ObjType.AV,
        "Air direction setting", Kind.COMMAND,
    ),
    Member(
        23, "air_direction_status", "AirDirectionStatus", ObjType.AI,
        "Air direction",
        note="0-3 horizontal, 4 vertical, 7 swing. Not a contiguous "
             "enumeration, so it is carried as a number with a label.",
    ),
    Member(
        24, "forced_thermo_off_command", "ForcedThermoOFFCommand", ObjType.BO,
        "Forced thermostat off setting", Kind.COMMAND,
        states=("Clearance", "Set"), gated=True,
    ),
    Member(
        25, "forced_thermo_off_status", "ForcedThermoOFFStatus", ObjType.BI,
        "Forced thermostat off", states=("Clearance", "Set"),
    ),
    Member(
        26, "energy_saving_command", "EnergyEfficiencyCommand", ObjType.BO,
        "Energy saving setting", Kind.COMMAND, states=("Clearance", "Set"),
    ),
    Member(
        27, "energy_saving_status", "EnergyEfficiencyStatus", ObjType.BI,
        "Energy saving", states=("Clearance", "Set"),
    ),
    Member(
        28, "thermo_status", "ThermoStatus", ObjType.BI,
        "Thermostat", states=("Off", "On"), default_on=True,
    ),
    Member(
        29, "compressor_status", "CompressorStatus", ObjType.BI,
        "Compressor", states=("Stopped", "Running"),
        note="The compressor of the outdoor unit serving this indoor unit. "
             "It is the only outdoor information the gateway publishes.",
    ),
    Member(
        30, "indoor_fan_status", "IndoorFanStatus", ObjType.BI,
        "Indoor fan", states=("Stopped", "Running"),
    ),
    Member(
        31, "heater_status", "HeaterStatus", ObjType.BI,
        "Heater", states=("Stopped", "Running"),
    ),
    Member(
        32, "ventilation_mode_command", "VentilationModeCommand", ObjType.MO,
        "Ventilation mode setting", Kind.COMMAND,
        states=("Bypass", "Heat exchange", "Auto"),
    ),
    Member(
        33, "ventilation_mode_status", "VentilationModeStatus", ObjType.MI,
        "Ventilation mode", states=("Bypass", "Heat exchange", "Auto"),
    ),
    Member(
        34, "ventilation_amount_command", "VentilationAmountCommand", ObjType.MO,
        "Ventilation amount setting", Kind.COMMAND,
        states=("Low", "High", "Auto", "Low (fresh up)", "High (fresh up)",
                "Auto (fresh up)"),
    ),
    Member(
        35, "ventilation_amount_status", "VentilationAmountStatus", ObjType.MI,
        "Ventilation amount",
        states=("Low", "High", "Auto", "Low (fresh up)", "High (fresh up)",
                "Auto (fresh up)"),
    ),
)

MEMBERS: Final = MappingProxyType({m.number: m for m in _MEMBERS})
MEMBERS_BY_KEY: Final = MappingProxyType({m.key: m for m in _MEMBERS})

#: Vacant member numbers, kept explicit so a gap is never read as an omission.
VACANT: Final = MappingProxyType({
    15: "vacant",
    18: "Japanese market only",
})

#: Members read-only enough to expose before the write path exists.
READABLE: Final = tuple(
    m for m in _MEMBERS if m.kind is not Kind.RESET
)

#: What a fresh config entry enables.
DEFAULT_MEMBERS: Final = tuple(m.key for m in _MEMBERS if m.default_on)

#: The four worth subscribing to rather than waiting for a sweep.
COV_MEMBERS: Final = tuple(m.key for m in _MEMBERS if m.cov)

#: Probed during discovery to decide whether a group exists at all. Every
#: gateway publishes it for every group it knows about.
PROBE_MEMBER: Final = 2


AIR_DIRECTION_LABELS: Final = MappingProxyType({
    0: "Horizontal",
    1: "Horizontal",
    2: "Horizontal",
    3: "Horizontal",
    4: "Vertical",
    7: "Swing",
})


def member_state(member: Member, value: object) -> str | None:
    """Label a Present_Value, or None if it cannot be labelled.

    Daikin's multi-state values are 1-based and binary objects arrive as
    0/1 or as an enumeration name. An out-of-range value is returned as its
    own string rather than guessed at: a wrong label is worse than a number.
    """
    if value is None:
        return None
    if member.key == "air_direction_status":
        try:
            return AIR_DIRECTION_LABELS.get(int(value), str(value))
        except (TypeError, ValueError):
            return str(value)
    if not member.states:
        return None
    index = _as_state_index(member, value)
    if index is None:
        return str(value)
    if 1 <= index <= len(member.states):
        return member.states[index - 1] or str(value)
    return str(value)


def _as_state_index(member: Member, value: object) -> int | None:
    """Turn a Present_Value into a 1-based index into member.states."""
    if member.object_type in (ObjType.BI, ObjType.BO, ObjType.BV):
        # Binary objects are inactive/active, which is states[0]/states[1].
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("inactive", "0", "false", "off"):
                return 1
            if lowered in ("active", "1", "true", "on"):
                return 2
            return None
        try:
            return 1 if int(value) == 0 else 2
        except (TypeError, ValueError):
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_binary_active(value: object) -> bool | None:
    """Whether a binary Present_Value is active, or None if undecidable."""
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("active", "1", "true", "on"):
            return True
        if lowered in ("inactive", "0", "false", "off"):
            return False
        return None
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class PointRef:
    """A single point on a single group, and how to ask for it."""

    ac_number: int
    member: Member

    @property
    def instance(self) -> int:
        return instance_of(self.ac_number, self.member.number)

    @property
    def object_id(self) -> tuple[str, int]:
        """The bacpypes3 object identifier tuple."""
        return (str(self.member.object_type), self.instance)

    @property
    def object_name(self) -> str:
        """Daikin's own name for this object, with the group suffix."""
        return f"{self.member.object_name}_{self.ac_number}"

    @property
    def key(self) -> str:
        return f"{self.ac_number}_{self.member.key}"


def points_for(ac_number: int, member_keys: object = None) -> list[PointRef]:
    """The points of one group, optionally limited to some member keys."""
    if member_keys is None:
        chosen = READABLE
    else:
        wanted = set(member_keys)  # type: ignore[arg-type]
        chosen = tuple(m for m in READABLE if m.key in wanted)
    return [PointRef(ac_number, m) for m in chosen]
