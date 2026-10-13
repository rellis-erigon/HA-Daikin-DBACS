"""A synthetic Daikin gateway, generated from the instance formula.

Nothing here comes from a real installation, which is the point: the
instance numbering is computable, so a complete and realistic 256-group
gateway can be built from arithmetic and the suite never needs site data.

`FakeApp` stands in for a bacpypes3 Application, so the real
:class:`DbacsGateway` is the thing under test — its chunking, its error
mapping and its partial-failure behaviour — rather than a mock of itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from custom_components.daikin_dbacs.objects import (
    MEMBERS,
    instance_of,
    split_instance,
)


class FakePriorityValue:
    """A Priority_Array slot. Empty slots carry BACnet Null."""

    def __init__(self, held: bool) -> None:
        self.null = None if held else object()
        self.real = 1.0 if held else None


def _priority_array(held_slots: list[int]) -> list[FakePriorityValue]:
    return [FakePriorityValue(slot in held_slots) for slot in range(1, 17)]


def _split_identifier(object_id) -> tuple[str, int]:
    """Accept the forms bacpypes3 does: "type,instance", or an indexable."""
    if isinstance(object_id, str):
        type_name, _, instance = object_id.partition(",")
        return type_name, int(instance)
    return str(object_id[0]), int(object_id[1])


class FakeError:
    """Stands in for a BACnet ErrorType inside an RPM result."""

    def __init__(self, error_class: str, error_code: str) -> None:
        self.errorClass = error_class  # noqa: N815 - mirrors bacpypes3
        self.errorCode = error_code  # noqa: N815 - mirrors bacpypes3

    def __repr__(self) -> str:
        return f"FakeError({self.errorClass}/{self.errorCode})"


class FakeReject(FakeError):
    """Stands in for a whole-request ErrorRejectAbortNack."""


@dataclass
class FakeIAm:
    iAmDeviceIdentifier: tuple[str, int]  # noqa: N815 - mirrors bacpypes3


@dataclass
class FakeGatewayState:
    """The synthetic gateway's contents and behaviour."""

    device_instance: int = 2001
    #: Groups the gateway publishes objects for. Everything else answers
    #: unknown-object, which is how a real gateway says "nothing is wired
    #: there".
    populated: set[int] = field(default_factory=lambda: set(range(0, 48)))
    #: Published groups whose DIII-NET side is faulted.
    dead: set[int] = field(default_factory=set)
    #: Groups whose faults are reported.
    faulted: set[int] = field(default_factory=set)
    database_revision: int = 7
    system_status: str = "operational"
    #: Set to raise CONFIGURATION_IN_PROGRESS until it reaches zero.
    scanning_for: int = 0
    #: Fail every request, as an unreachable gateway would.
    offline: bool = False
    #: Whether a unicast Who-Is is answered. Real gateways often do not,
    #: because I-Am is broadcast and cannot cross a routed boundary.
    answers_who_is: bool = True
    #: Whether the device object can be read at the wildcard instance.
    answers_wildcard: bool = True
    #: Whether the gateway gives up its Object_List at all.
    serves_object_list: bool = True
    #: Which members each group publishes. None means all of them. Real
    #: gateways differ group to group, and by firmware.
    members: dict[int, set[int]] | None = None
    #: Objects the integration does not model, as (type, instance) pairs.
    #: Member 18 is real, Japanese-market-only, and turns up in the wild.
    extra_objects: list[tuple[str, int]] = field(default_factory=list)
    #: Object instances that should answer with an error instead of a value.
    broken: set[int] = field(default_factory=set)
    #: How many subscriptions to accept before refusing, as the gateway does
    #: once its five recipients per object are taken.
    cov_budget: int = 1000

    #: Priority slots another client is holding, per object instance. A real
    #: gateway shares its objects with a building management head-end.
    priorities: dict[int, list[int]] = field(default_factory=dict)
    #: Refuse every write, as a gateway with a write-inhibited point would.
    refuse_writes: bool = False

    rpm_calls: list[list[Any]] = field(default_factory=list)
    writes: list[tuple[str, int, Any, int | None]] = field(default_factory=list)
    subscriptions: list[tuple[str, int]] = field(default_factory=list)
    #: Change-of-value notifications waiting to be delivered. A test pushes
    #: (property-identifier, value) pairs here the way the gateway would.
    notifications: Any = None

    def __post_init__(self) -> None:
        import asyncio  # noqa: PLC0415

        if self.notifications is None:
            self.notifications = asyncio.Queue()

    def notify(self, value: Any, prop: str = "present-value") -> None:
        """Queue a notification, as the gateway sends one when a point moves."""
        self.notifications.put_nowait((prop, value))

    def published(self, ac_number: int, member_number: int) -> bool:
        """Whether this group publishes this member."""
        if ac_number not in self.populated:
            return False
        if self.members is None:
            return member_number in MEMBERS
        return member_number in self.members.get(ac_number, set())

    def object_list(self) -> list[tuple[str, int]]:
        """The gateway's Object_List, as it would report it."""
        objects: list[tuple[str, int]] = [("device", self.device_instance)]
        for ac_number in sorted(self.populated):
            for member_number in sorted(MEMBERS):
                if not self.published(ac_number, member_number):
                    continue
                member = MEMBERS[member_number]
                objects.append(
                    (
                        str(member.object_type),
                        instance_of(ac_number, member_number),
                    )
                )
        objects.extend(self.extra_objects)
        return objects

    def present_value(self, instance: int) -> Any:
        """A plausible Present_Value for a point, derived from its address."""
        ac_number, member_number = split_instance(instance)
        member = MEMBERS.get(member_number)
        if member is None:
            return None
        key = member.key
        if key == "communication_status":
            # ACTIVE means communicating abnormally.
            return 1 if ac_number in self.dead else 0
        if key == "alarm":
            return 1 if ac_number in self.faulted else 0
        if key == "malfunction_code":
            return 42 if ac_number in self.faulted else 1
        if key == "room_temp":
            return 20.0 + (ac_number % 7) * 0.5
        if key == "setpoint":
            return 21.0 + (ac_number % 4)
        if key == "mode_status":
            return 1 + (ac_number % 3)
        if key == "fan_status":
            return 1 + (ac_number % 4)
        if key == "on_off_status":
            return ac_number % 2
        if key == "air_direction_status":
            return (0, 4, 7)[ac_number % 3]
        if key == "accumulated_power":
            return float(ac_number * 10)
        if member.states:
            return 0
        return 0.0


class FakeApp:
    """The subset of a bacpypes3 Application that DbacsGateway uses."""

    def __init__(self, state: FakeGatewayState) -> None:
        self.state = state
        self.closed = False

    # --- identity ------------------------------------------------------------

    async def who_is(self, address=None, **_kwargs):
        if self.state.offline:
            raise TimeoutError("no answer")
        if not self.state.answers_who_is:
            # bacpypes3 returns an empty list rather than raising when no
            # I-Am arrives, which is what a broadcast-only I-Am looks like.
            return []
        return [FakeIAm(("device", self.state.device_instance))]

    # --- reads ---------------------------------------------------------------

    async def read_property(
        self, _address, objid, prop, array_index=None, **_kwargs
    ):
        # bacpypes3 *returns* an error object in place of the value rather
        # than raising it, so the fake does the same or it would hide the
        # one mistake worth catching here.
        if self.state.offline:
            raise TimeoutError("no answer")
        if self.state.scanning_for > 0:
            self.state.scanning_for -= 1
            return FakeReject("device", "configuration-in-progress")
        if str(prop) == "object-list":
            if not self.state.serves_object_list:
                return FakeError("property", "unknown-property")
            objects = self.state.object_list()
            if array_index == 0:
                return len(objects)
            if array_index is not None:
                if 1 <= array_index <= len(objects):
                    return objects[array_index - 1]
                return FakeError("property", "invalid-array-index")
            return objects
        if str(prop) == "object-identifier":
            if not self.state.answers_wildcard:
                return FakeError("object", "unknown-object")
            return ("device", self.state.device_instance)
        return None

    async def read_property_multiple(self, _address, parameter_list, **_kwargs):
        if self.state.offline:
            raise TimeoutError("no answer")

        # bacpypes3 takes the parameter list flat - object, properties,
        # object, properties - whatever its type annotation says, so the fake
        # insists on the same shape.
        if len(parameter_list) % 2:
            raise ValueError("read access specification expected")
        pairs = list(
            zip(parameter_list[::2], parameter_list[1::2], strict=True)
        )
        self.state.rpm_calls.append(pairs)

        if self.state.scanning_for > 0:
            self.state.scanning_for -= 1
            return FakeReject("device", "configuration-in-progress")

        results: list[tuple] = []
        for object_id, properties in pairs:
            type_name, instance = _split_identifier(object_id)
            # A real response carries an ObjectIdentifier, which indexes
            # like a (type, instance) pair, not the string the request used.
            answered_as = (type_name, instance)
            for prop in properties:
                name = str(prop)
                if name == "priority-array":
                    results.append(
                        (
                            answered_as,
                            name,
                            None,
                            _priority_array(
                                self.state.priorities.get(instance, [])
                            ),
                        )
                    )
                    continue
                if name != "present-value":
                    results.append(
                        (answered_as, name, None, self._device_property(name))
                    )
                    continue
                results.append(
                    (answered_as, name, None, self._value(type_name, instance))
                )
        return results

    def _device_property(self, name: str) -> Any:
        values = {
            "object-name": "D-BACS-TEST",
            "vendor-name": "Daikin Industries",
            "model-name": "DMS502B71",
            "firmware-revision": "6.34.00",
            "application-software-version": "6.34.00",
            "system-status": self.state.system_status,
            "database-revision": self.state.database_revision,
            "protocol-revision": 4,
            "max-apdu-length-accepted": 1024,
            "segmentation-supported": "segmented-both",
            "description": "U1",
        }
        return values.get(name)

    def _value(self, _type_name: str, instance: int) -> Any:
        if instance in self.state.broken:
            return FakeError("property", "write-access-denied")
        ac_number, member_number = split_instance(instance)
        if not self.state.published(ac_number, member_number):
            return FakeError("object", "unknown-object")
        return self.state.present_value(instance)

    # --- change of value -----------------------------------------------------

    async def write_property(
        self, _address, objid, prop, value, array_index=None, priority=None
    ):
        if self.state.offline:
            raise TimeoutError("no answer")
        type_name, instance = _split_identifier(objid)
        if self.state.refuse_writes:
            return FakeError("property", "write-access-denied")
        self.state.writes.append((type_name, instance, value, priority))
        if priority is not None:
            held = self.state.priorities.setdefault(instance, [])
            if priority not in held:
                held.append(priority)
        return None

    def change_of_value(self, _address, object_identifier, **_kwargs):
        return _FakeSubscription(self.state, object_identifier)

    def close(self) -> None:
        self.closed = True


class _FakeSubscription:
    def __init__(self, state: FakeGatewayState, object_identifier) -> None:
        self.state = state
        self.object_identifier = object_identifier

    async def __aenter__(self):
        if len(self.state.subscriptions) >= self.state.cov_budget:
            raise RuntimeError("cov-subscription-failed")
        self.state.subscriptions.append(
            _split_identifier(self.object_identifier)
        )
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get_value(self):
        """One (property, value) pair, exactly as bacpypes3 returns it.

        Nothing arrives unless a test pushes it onto the state's queue: a
        subscription that invented traffic would hide a sweep that was never
        happening.
        """
        return await self.state.notifications.get()


def attach(gateway, state: FakeGatewayState) -> FakeApp:
    """Point a real DbacsGateway at a synthetic one."""
    app = FakeApp(state)
    gateway._app = app  # noqa: SLF001 - the seam the fake exists to use
    gateway._address = "fake"  # noqa: SLF001
    gateway.device_instance = state.device_instance
    return app


def instances_in(parameter_list) -> list[int]:
    """The object instances one RPM call asked about."""
    return [
        _split_identifier(object_id)[1] for object_id, _props in parameter_list
    ]


def expected_instance(ac_number: int, member_key: str) -> int:
    from custom_components.daikin_dbacs.objects import MEMBERS_BY_KEY  # noqa: PLC0415

    return instance_of(ac_number, MEMBERS_BY_KEY[member_key].number)
