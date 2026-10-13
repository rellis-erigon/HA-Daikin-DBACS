"""The BACnet/IP client, and the only module that knows bacpypes3 exists.

Everything above this talks to :class:`DbacsGateway`, which means the
coordinator and the platforms can be tested against a fake gateway rather
than a BACnet stack. The error vocabulary is deliberately small and
deliberately not BACnet's: callers care whether a point is *absent*, whether
the gateway is *not ready yet*, or whether it is *unreachable*, and those
three are the distinctions the whole integration is built on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .const import (
    CONFIG_IN_PROGRESS_BACKOFF,
    CONFIG_IN_PROGRESS_RETRIES,
    DEFAULT_LOCAL_PORT,
    DEFAULT_OUR_INSTANCE,
    DEFAULT_PORT,
    MAX_CONCURRENCY,
    REQUEST_TIMEOUT,
    RPM_CHUNK,
)
from .objects import MEMBERS, PROBE_MEMBER, PointRef

_LOGGER = logging.getLogger(__name__)


class DbacsError(Exception):
    """Base class for anything that went wrong talking to the gateway."""


class DbacsUnreachable(DbacsError):
    """The gateway did not answer at all."""


class DbacsNotReady(DbacsError):
    """The gateway is scanning its DIII-NET ports.

    Distinct from unreachable on purpose: the answer is "not yet", and
    treating it as "nothing there" would write off every group on the system.
    """


class DbacsUnknownObject(DbacsError):
    """The gateway has no such object — the group is not populated."""


class DbacsProtocolError(DbacsError):
    """The gateway answered, but not with something we can use."""


# Daikin's documented error responses mapped to ours, keyed on the error code
# alone: the code is the distinguishing part, and bacpypes3 renders these
# hyphenated while the standard writes them camelCase, so both are accepted.
_ERROR_MAP: dict[str, type[DbacsError]] = {
    "configuration-in-progress": DbacsNotReady,
    "configurationInProgress": DbacsNotReady,
    "unknown-object": DbacsUnknownObject,
    "unknownObject": DbacsUnknownObject,
    "unknown-property": DbacsUnknownObject,
    "unknownProperty": DbacsUnknownObject,
}


@dataclass(frozen=True, slots=True)
class GatewayInfo:
    """What the gateway says about itself."""

    device_instance: int
    object_name: str | None = None
    vendor_name: str | None = None
    model_name: str | None = None
    firmware_revision: str | None = None
    application_software_version: str | None = None
    system_status: str | None = None
    database_revision: int | None = None
    protocol_revision: int | None = None
    max_apdu_length: int | None = None
    segmentation_supported: str | None = None

    @property
    def restart_token(self) -> tuple[Any, ...]:
        """A value that changes when the gateway has restarted.

        Change-of-value subscriptions do not survive a power cycle and the
        gateway does not mention it, so this is how the coordinator notices.
        """
        return (self.database_revision, self.system_status)


@dataclass(slots=True)
class PointValue:
    """One reading, with whatever went wrong instead of a value."""

    point: PointRef
    value: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class _Subscription:
    point: PointRef
    callback: Callable[[PointRef, Any], None]
    task: asyncio.Task | None = None
    context: Any = None


class DbacsGateway:
    """A BACnet/IP client pointed at one Daikin BACnet interface.

    Unicast only. The address is given, so nothing is discovered by
    broadcast, which is what lets this work across subnets without a BBMD
    and keeps us quiet on a network that has a head-end on it.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        device_instance: int | None = None,
        local_address: str | None = None,
        local_port: int = DEFAULT_LOCAL_PORT,
        our_instance: int = DEFAULT_OUR_INSTANCE,
    ) -> None:
        self.host = host
        self.port = port
        self.device_instance = device_instance
        self.local_address = local_address
        self.local_port = local_port
        self.our_instance = our_instance

        self._app: Any = None
        self._address: Any = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        self._subscriptions: list[_Subscription] = []

    # --- lifecycle -----------------------------------------------------------

    async def connect(self) -> GatewayInfo:
        """Bind a local BACnet device and identify the gateway."""
        # Imported here so that importing this module does not require the
        # BACnet stack — the object model and its tests must stay portable.
        from bacpypes3.app import Application  # noqa: PLC0415
        from bacpypes3.local.device import DeviceObject  # noqa: PLC0415
        from bacpypes3.local.networkport import NetworkPortObject  # noqa: PLC0415
        from bacpypes3.pdu import Address  # noqa: PLC0415

        bind_to = self.local_address or "0.0.0.0/0"
        if ":" not in bind_to:
            bind_to = f"{bind_to}:{self.local_port}"

        device = DeviceObject(
            objectIdentifier=("device", self.our_instance),
            objectName=f"HomeAssistant-{self.our_instance}",
            vendorIdentifier=0,
            maxApduLengthAccepted=1024,
            segmentationSupported="segmentedBoth",
        )
        network_port = NetworkPortObject(
            bind_to,
            objectIdentifier=("network-port", 1),
            objectName="NetworkPort-1",
        )

        try:
            self._app = Application.from_object_list([device, network_port])
        except OSError as err:
            raise DbacsUnreachable(
                f"could not bind local BACnet port {bind_to}: {err}"
            ) from err

        self._address = Address(f"{self.host}:{self.port}")
        try:
            return await self._identify()
        except DbacsError:
            await self.close()
            raise

    async def close(self) -> None:
        """Cancel every subscription and release the local port."""
        await self.unsubscribe_all()
        if self._app is not None:
            with suppress(Exception):
                self._app.close()
            self._app = None
        self._address = None

    @property
    def connected(self) -> bool:
        return self._app is not None

    # --- identity ------------------------------------------------------------

    async def read_info(self) -> GatewayInfo:
        """Re-read the device object, to see whether it has restarted."""
        return await self._identify()

    async def _identify(self) -> GatewayInfo:
        """Learn the gateway's device instance, then read its device object."""
        instance = self.device_instance
        if instance is None:
            instance = await self._who_is()
        self.device_instance = instance

        device_id = ("device", instance)
        wanted = (
            "object-name",
            "vendor-name",
            "model-name",
            "firmware-revision",
            "application-software-version",
            "system-status",
            "database-revision",
            "protocol-revision",
            "max-apdu-length-accepted",
            "segmentation-supported",
        )
        values: dict[str, Any] = {}
        for name, value in await self._read_properties(device_id, wanted):
            values[name] = value

        return GatewayInfo(
            device_instance=instance,
            object_name=_text(values.get("object-name")),
            vendor_name=_text(values.get("vendor-name")),
            model_name=_text(values.get("model-name")),
            firmware_revision=_text(values.get("firmware-revision")),
            application_software_version=_text(
                values.get("application-software-version")
            ),
            system_status=_text(values.get("system-status")),
            database_revision=_int(values.get("database-revision")),
            protocol_revision=_int(values.get("protocol-revision")),
            max_apdu_length=_int(values.get("max-apdu-length-accepted")),
            segmentation_supported=_text(values.get("segmentation-supported")),
        )

    async def _who_is(self) -> int:
        """Unicast Who-Is, to learn the device instance we were not given."""
        try:
            async with self._semaphore:
                found = await asyncio.wait_for(
                    self._app.who_is(address=self._address),
                    timeout=REQUEST_TIMEOUT,
                )
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise DbacsUnreachable(
                f"no I-Am from {self.host}:{self.port}. If the gateway is "
                "there, its device instance number has to be given by hand."
            ) from err
        except OSError as err:
            raise DbacsUnreachable(f"{self.host}:{self.port}: {err}") from err

        for i_am in found or []:
            identifier = getattr(i_am, "iAmDeviceIdentifier", None)
            if identifier is not None:
                return int(identifier[1])
        raise DbacsUnreachable(
            f"{self.host}:{self.port} answered Who-Is without a device "
            "identifier"
        )

    # --- reads ---------------------------------------------------------------

    async def _read_properties(
        self, object_id: tuple[str, int], properties: Sequence[str]
    ) -> list[tuple[str, Any]]:
        """ReadPropertyMultiple for one object, errors dropped per property."""
        results = await self._rpm([(object_id, list(properties))])
        return [
            (str(prop), value)
            for (_obj, prop, _index, value) in results
            if not _is_error(value)
        ]

    async def _rpm(self, parameter_list: list[Any]) -> list[tuple[Any, ...]]:
        """One ReadPropertyMultiple, with our error vocabulary applied.

        bacpypes3 *returns* a failure rather than raising it, and a
        per-property failure arrives inside the result list, so both have to
        be unpicked here rather than anywhere else.
        """
        if self._app is None:
            raise DbacsUnreachable("not connected")

        try:
            async with self._semaphore:
                response = await asyncio.wait_for(
                    self._app.read_property_multiple(
                        self._address, parameter_list
                    ),
                    timeout=REQUEST_TIMEOUT,
                )
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise DbacsUnreachable(
                f"{self.host} did not answer within {REQUEST_TIMEOUT}s"
            ) from err
        except OSError as err:
            raise DbacsUnreachable(f"{self.host}: {err}") from err

        if response is None:
            raise DbacsProtocolError(f"{self.host} gave no usable response")
        if _is_failure(response):
            raise _map_error(response)
        if not isinstance(response, (list, tuple)):
            raise DbacsProtocolError(
                f"{self.host} answered with {type(response).__name__}, "
                "which is not a list of results"
            )
        return list(response)

    async def read_points(
        self, points: Iterable[PointRef]
    ) -> dict[str, PointValue]:
        """Read Present_Value for many points, batched per group.

        Points are grouped by indoor unit so that one unit's readings arrive
        together and a card can never show half a stale unit. A chunk that
        fails is recorded against its own points and the sweep carries on:
        one unresponsive group must not cost us the other 255.
        """
        by_unit: dict[int, list[PointRef]] = {}
        for point in points:
            by_unit.setdefault(point.ac_number, []).append(point)

        chunks: list[list[PointRef]] = []
        for unit_points in by_unit.values():
            for start in range(0, len(unit_points), RPM_CHUNK):
                chunks.append(unit_points[start : start + RPM_CHUNK])

        results: dict[str, PointValue] = {}
        gathered = await asyncio.gather(
            *(self._read_chunk(chunk) for chunk in chunks),
            return_exceptions=True,
        )
        for chunk, outcome in zip(chunks, gathered, strict=True):
            if isinstance(outcome, DbacsNotReady):
                raise outcome
            if isinstance(outcome, BaseException):
                reason = _reason(outcome)
                for point in chunk:
                    results[point.key] = PointValue(point, error=reason)
                continue
            results.update(outcome)
        return results

    async def _read_chunk(
        self, chunk: Sequence[PointRef]
    ) -> dict[str, PointValue]:
        parameter_list = [
            (point.object_id, ["present-value"]) for point in chunk
        ]
        results = await self._rpm(parameter_list)

        by_instance: dict[int, PointRef] = {
            point.instance: point for point in chunk
        }
        out: dict[str, PointValue] = {}
        for object_id, _prop, _index, value in results:
            point = by_instance.get(int(object_id[1]))
            if point is None:
                continue
            if _is_error(value):
                out[point.key] = PointValue(point, error=_error_text(value))
            else:
                out[point.key] = PointValue(point, value=value)

        # Anything the gateway simply left out is still unanswered, and
        # saying so is better than leaving a hole for someone else to find.
        for point in chunk:
            out.setdefault(
                point.key, PointValue(point, error="no result returned")
            )
        return out

    async def read_description(self, point: PointRef) -> str | None:
        """The Description property, which is where a malfunction code lives.

        Daikin puts the two-character error code (``U1``, ``E7``) in the
        object's description rather than in its value, so a fault is two
        reads: what changed, then what it says.
        """
        results = await self._read_properties(point.object_id, ["description"])
        for _name, value in results:
            text = _text(value)
            if text:
                return text
        return None

    async def probe_groups(
        self, ac_numbers: Iterable[int]
    ) -> dict[int, bool]:
        """Which of these indoor-unit groups the gateway actually publishes.

        One cheap point per group, batched. ``UNKNOWN_OBJECT`` is the
        gateway telling us a slot is empty — the published point list covers
        all 256 whether or not anything is wired to them, so this is the only
        honest way to tell a real group from a gap.
        """
        probe = MEMBERS[PROBE_MEMBER]
        points = [PointRef(ac, probe) for ac in ac_numbers]
        readings = await self.read_points(points)
        found: dict[int, bool] = {}
        for point in points:
            reading = readings.get(point.key)
            found[point.ac_number] = bool(reading and reading.ok)
        return found

    async def read_object_list_count(self) -> int | None:
        """How many objects the gateway holds, for diagnostics only.

        Index 0 of an array property is its length, so this costs one read
        rather than the ~45 KB a full Object_List would segment into.
        """
        if self._app is None:
            raise DbacsUnreachable("not connected")
        try:
            async with self._semaphore:
                value = await asyncio.wait_for(
                    self._app.read_property(
                        self._address,
                        ("device", self.device_instance),
                        "object-list",
                        array_index=0,
                    ),
                    timeout=REQUEST_TIMEOUT,
                )
        except (TimeoutError, asyncio.TimeoutError, OSError):
            return None
        return _int(value)

    async def with_retry_when_not_ready(self, coro_factory):
        """Run something, waiting out a gateway that is still scanning D3.

        ``CONFIGURATION_IN_PROGRESS`` means the gateway is building its own
        picture of the DIII-NET network. It will answer properly in a few
        seconds; the only wrong move is to conclude the system is empty.
        """
        delay = CONFIG_IN_PROGRESS_BACKOFF
        for attempt in range(CONFIG_IN_PROGRESS_RETRIES):
            try:
                return await coro_factory()
            except DbacsNotReady:
                if attempt == CONFIG_IN_PROGRESS_RETRIES - 1:
                    raise
                _LOGGER.debug(
                    "%s is still scanning DIII-NET; retrying in %ss",
                    self.host,
                    delay,
                )
                await asyncio.sleep(delay)
        raise DbacsNotReady(f"{self.host} never finished scanning DIII-NET")

    # --- change of value -----------------------------------------------------

    async def subscribe(
        self,
        point: PointRef,
        callback: Callable[[PointRef, Any], None],
        lifetime: int,
    ) -> bool:
        """Subscribe to one point, returning whether the gateway accepted.

        A refusal is not an error worth failing a setup over — the gateway
        allows five recipients per object and something else may already hold
        them. The caller falls back to polling that point.
        """
        if self._app is None:
            raise DbacsUnreachable("not connected")

        from bacpypes3.primitivedata import ObjectIdentifier  # noqa: PLC0415

        subscription = _Subscription(point=point, callback=callback)
        started: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        async def run() -> None:
            try:
                context = self._app.change_of_value(
                    self._address,
                    ObjectIdentifier(point.object_id),
                    lifetime=lifetime,
                )
                async with context as scm:
                    if not started.done():
                        started.set_result(True)
                    while True:
                        # get_value returns one (property, value) pair, not a
                        # list of them, and the gateway notifies on several
                        # properties of an object.
                        name, value = await scm.get_value()
                        if str(name) == "present-value":
                            callback(point, value)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - reported, not swallowed
                if not started.done():
                    started.set_result(False)
                _LOGGER.debug(
                    "change-of-value on %s failed, falling back to polling: %s",
                    point.object_name,
                    err,
                )

        subscription.task = asyncio.create_task(run())
        self._subscriptions.append(subscription)
        try:
            return await asyncio.wait_for(started, timeout=REQUEST_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            return False

    async def unsubscribe_all(self) -> None:
        """Cancel our subscriptions, so the gateway's slots are given back."""
        subscriptions, self._subscriptions = self._subscriptions, []
        for subscription in subscriptions:
            if subscription.task is not None:
                subscription.task.cancel()
        for subscription in subscriptions:
            if subscription.task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await subscription.task

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)


# --- helpers -----------------------------------------------------------------


def _is_error(value: Any) -> bool:
    """Whether this is a BACnet error rather than a value.

    Duck-typed on purpose. bacpypes3 reports a whole-request failure by
    returning an ErrorRejectAbortNack and a single-property failure by
    putting an ErrorType in the result, and recognising both by shape keeps
    this module importable — and testable — without the BACnet stack present.
    """
    return hasattr(value, "errorClass") and hasattr(value, "errorCode")


def _is_failure(value: Any) -> bool:
    """Whether a response is a failure rather than a list of results."""
    return _is_error(value) or hasattr(value, "apduAbortRejectReason")


def _error_text(value: Any) -> str:
    return f"{value.errorClass}/{value.errorCode}"


def _map_error(response: Any) -> DbacsError:
    """Turn a BACnet error, reject or abort into one of ours."""
    error_class = str(getattr(response, "errorClass", "") or "")
    error_code = str(getattr(response, "errorCode", "") or "")
    mapped = _ERROR_MAP.get(error_code)
    if mapped is not None:
        return mapped(f"{error_class}/{error_code}")
    if error_class or error_code:
        return DbacsProtocolError(f"{error_class}/{error_code}")
    reason = getattr(response, "apduAbortRejectReason", None)
    return DbacsProtocolError(f"rejected: {reason or type(response).__name__}")


def _reason(err: BaseException) -> str:
    text = str(err).strip()
    return text or type(err).__name__


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
