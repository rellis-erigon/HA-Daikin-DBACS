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
    READABLE,
    instance_of,
    split_instance,
)


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
    #: Object instances that should answer with an error instead of a value.
    broken: set[int] = field(default_factory=set)
    #: How many subscriptions to accept before refusing, as the gateway does
    #: once its five recipients per object are taken.
    cov_budget: int = 1000

    rpm_calls: list[list[Any]] = field(default_factory=list)
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
        return [FakeIAm(("device", self.state.device_instance))]

    # --- reads ---------------------------------------------------------------

    async def read_property(
        self, _address, objid, prop, array_index=None, **_kwargs
    ):
        if self.state.offline:
            raise TimeoutError("no answer")
        if str(prop) == "object-list" and array_index == 0:
            return len(self.state.populated) * len(READABLE)
        return None

    async def read_property_multiple(self, _address, parameter_list, **_kwargs):
        if self.state.offline:
            raise TimeoutError("no answer")
        self.state.rpm_calls.append(parameter_list)

        if self.state.scanning_for > 0:
            self.state.scanning_for -= 1
            return FakeReject("device", "configuration-in-progress")

        results: list[tuple] = []
        for object_id, properties in parameter_list:
            type_name, instance = str(object_id[0]), int(object_id[1])
            for prop in properties:
                name = str(prop)
                if name != "present-value":
                    results.append(
                        (object_id, name, None, self._device_property(name))
                    )
                    continue
                results.append(
                    (object_id, name, None, self._value(type_name, instance))
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
        ac_number, _member = split_instance(instance)
        if ac_number not in self.state.populated:
            return FakeError("object", "unknown-object")
        return self.state.present_value(instance)

    # --- change of value -----------------------------------------------------

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
            (str(self.object_identifier[0]), int(self.object_identifier[1]))
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
    return [int(object_id[1]) for object_id, _props in parameter_list]


def expected_instance(ac_number: int, member_key: str) -> int:
    from custom_components.daikin_dbacs.objects import MEMBERS_BY_KEY  # noqa: PLC0415

    return instance_of(ac_number, MEMBERS_BY_KEY[member_key].number)
