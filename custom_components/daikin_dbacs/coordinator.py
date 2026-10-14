"""Discovery, sweeping and change-of-value for one gateway."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_CONTROL,
    COV_MIN_LIFETIME,
    COV_REFRESH_SPREAD,
    DEFAULT_COV_LIFETIME,
    DEFAULT_MIN_WRITE_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WRITE_PRIORITY,
    DOMAIN,
    OPT_CONTROL,
    OPT_COV_LIFETIME,
    OPT_DAILY_WRITE_CAP,
    OPT_ENFORCE_WRITE_CAP,
    OPT_GATED,
    OPT_GROUPS,
    OPT_MEMBERS,
    OPT_MIN_WRITE_INTERVAL,
    OPT_SCAN_INTERVAL,
    OPT_USE_COV,
    OPT_WRITE_PRIORITY,
)
from .gateway import (
    DbacsError,
    DbacsGateway,
    DbacsNotReady,
    GatewayInfo,
    PointValue,
)
from .writes import CHANGES_PER_DAY, WriteGuard, WriteRefused
from .objects import (
    COV_MEMBERS,
    DEFAULT_MEMBERS,
    MAX_AC_NUMBER,
    MEMBERS_BY_KEY,
    PointRef,
    SELECTABLE,
    address_label,
    is_binary_active,
    points_for,
)

_LOGGER = logging.getLogger(__name__)

#: Every DIII-NET slot the gateway could publish. Discovery asks about all of
#: them because a group is cheap to ask about and expensive to miss.
ALL_AC_NUMBERS = tuple(range(MAX_AC_NUMBER + 1))


@dataclass(slots=True)
class GroupState:
    """What we know about one indoor-unit group."""

    ac_number: int
    present: bool = False
    """The gateway publishes objects for it."""
    communicating: bool | None = None
    """Its DIII-NET communication status, where we have read it."""
    members: frozenset[int] | None = None
    """Which member numbers this group publishes.

    None means we could not find out and have to assume the full set — the
    probing fallback cannot tell. Groups genuinely differ: two on a system
    may carry Forced stop while the rest do not, and older firmware carries
    none of the ventilation members.
    """

    def publishes(self, member_number: int) -> bool:
        return self.members is None or member_number in self.members

    @property
    def label(self) -> str:
        return address_label(self.ac_number)

    @property
    def live(self) -> bool:
        """Present and not reporting a communication fault.

        ``communicating is None`` counts as live: it means we have not read
        the point yet, and refusing to show a group we have simply not asked
        about would be its own kind of lie.
        """
        return self.present and self.communicating is not False


@dataclass(slots=True)
class Discovery:
    """The outcome of a discovery pass, kept for diagnostics as well as use."""

    groups: dict[int, GroupState] = field(default_factory=dict)
    absent: tuple[int, ...] = ()
    dead: tuple[int, ...] = ()
    object_count: int | None = None
    unrecognised: dict[str, int] = field(default_factory=dict)
    discovered_by: str = ""

    def members_of(self, ac_number: int) -> frozenset[int] | None:
        group = self.groups.get(ac_number)
        return None if group is None else group.members

    @property
    def live_numbers(self) -> tuple[int, ...]:
        return tuple(
            ac for ac, group in sorted(self.groups.items()) if group.live
        )


class DbacsCoordinator(DataUpdateCoordinator[dict[str, PointValue]]):
    """Keeps one gateway's points up to date.

    The sweep is the source of truth and the safety net. Change-of-value
    rides on top of it for the handful of points worth seeing move, and if a
    subscription is refused or lapses the point simply arrives on the next
    sweep instead — never silently stale.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        gateway: DbacsGateway,
        info: GatewayInfo,
    ) -> None:
        self.gateway = gateway
        self.info = info
        self.discovery = Discovery()
        self.entry = entry
        self.site: dict[int, Any] = {}
        """What the site mapping file says about each unit, where there is
        one. Held so a generated card can honour a per-unit faceplate
        choice."""
        self.systems: dict[str, Any] = {}
        """Refrigerant systems, from the site mapping file if there is one.

        The gateway cannot say which units share an outdoor unit, so without
        a mapping there are no systems — which is the honest outcome rather
        than a guess."""
        self.hub_device_id: str | None = None
        self._dead_sweeps = 0
        """The registry id of the gateway's own device, set at setup."""
        self._restart_token = info.restart_token
        self._cov_points: dict[str, PointRef] = {}
        self._cov_failed: set[str] = set()
        self._descriptions: dict[str, str | None] = {}
        self._priorities: dict[str, list[int]] = {}
        self._pending: dict[str, tuple[PointRef, Any]] = {}
        self._flushing: set[str] = set()
        self._held: dict[str, tuple[PointRef, int]] = {}
        """Points we have written and the priority we are holding them at.

        A BACnet write holds its slot until relinquished, so this is the
        record of what Home Assistant is keeping hold of."""
        self.guard = WriteGuard(
            min_interval=float(
                entry.options.get(
                    OPT_MIN_WRITE_INTERVAL, DEFAULT_MIN_WRITE_INTERVAL
                )
            ),
            daily_cap=int(entry.options.get(OPT_DAILY_WRITE_CAP, CHANGES_PER_DAY)),
            enforce_cap=bool(entry.options.get(OPT_ENFORCE_WRITE_CAP, True)),
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {gateway.host}",
            update_interval=timedelta(seconds=self.scan_interval),
            config_entry=entry,
        )

    # --- options -------------------------------------------------------------

    @property
    def scan_interval(self) -> int:
        return int(
            self.entry.options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )

    @property
    def enabled_member_keys(self) -> tuple[str, ...]:
        chosen = self.entry.options.get(OPT_MEMBERS)
        if not chosen:
            return DEFAULT_MEMBERS
        known = {member.key for member in SELECTABLE}
        return tuple(key for key in chosen if key in known)

    @property
    def selected_groups(self) -> tuple[int, ...] | None:
        """The groups the user has narrowed to, or None for "all that live"."""
        chosen = self.entry.options.get(OPT_GROUPS)
        if not chosen:
            return None
        return tuple(sorted(int(value) for value in chosen))

    @property
    def control_enabled(self) -> bool:
        """Whether this entry may write to the gateway at all."""
        return bool(self.entry.options.get(OPT_CONTROL, DEFAULT_CONTROL))

    @property
    def gated_enabled(self) -> bool:
        """Whether the points that lock occupants out may be exposed."""
        return bool(self.entry.options.get(OPT_GATED, False))

    @property
    def write_priority(self) -> int:
        return int(
            self.entry.options.get(OPT_WRITE_PRIORITY, DEFAULT_WRITE_PRIORITY)
        )

    @property
    def use_cov(self) -> bool:
        return bool(self.entry.options.get(OPT_USE_COV, True))

    @property
    def cov_lifetime(self) -> int:
        return int(
            self.entry.options.get(OPT_COV_LIFETIME, DEFAULT_COV_LIFETIME)
        )

    # --- discovery -----------------------------------------------------------

    async def async_discover(self) -> Discovery:
        """Find out which of the 256 slots are real, and which are dead.

        Three outcomes, kept apart because conflating them is how a dashboard
        ends up confidently wrong: *absent* (the gateway has no such object),
        *dead* (objects exist but DIII-NET communication is faulted), and
        live. Absent and dead groups get no Home Assistant device, and both
        are listed in diagnostics so a group that comes back is noticed.
        """
        object_map = await self.gateway.with_retry_when_not_ready(
            self.gateway.read_object_map
        )

        if object_map is not None:
            groups = {
                ac: GroupState(
                    ac_number=ac,
                    present=ac in object_map.members,
                    members=object_map.members.get(ac),
                )
                for ac in ALL_AC_NUMBERS
            }
            unrecognised = object_map.unrecognised
            object_count = object_map.total
            how = "its Object_List"
        else:
            # The gateway would not give up its Object_List. Probing each
            # slot still finds the groups; it just cannot tell which members
            # each one carries, so the full set is assumed.
            found = await self.gateway.with_retry_when_not_ready(
                lambda: self.gateway.probe_groups(ALL_AC_NUMBERS)
            )
            groups = {
                ac: GroupState(ac_number=ac, present=present)
                for ac, present in sorted(found.items())
            }
            unrecognised = {}
            object_count = await self.gateway.read_object_list_count()
            how = "probing every slot"

        present_numbers = [ac for ac, group in groups.items() if group.present]
        _LOGGER.debug(
            "%s publishes %d of %d possible groups, found by %s",
            self.gateway.host,
            len(present_numbers),
            len(ALL_AC_NUMBERS),
            how,
        )
        if unrecognised:
            _LOGGER.info(
                "%s publishes objects this integration does not model, which "
                "are ignored: %s",
                self.gateway.host,
                ", ".join(
                    f"{kind} x{count}"
                    for kind, count in sorted(unrecognised.items())
                ),
            )

        # A published group may still be wired to nothing that answers, so
        # ask each one how its DIII-NET side is doing before believing in it.
        comms_member = MEMBERS_BY_KEY["communication_status"]
        if present_numbers:
            readings = await self.gateway.read_points(
                PointRef(ac, comms_member) for ac in present_numbers
            )
            for ac in present_numbers:
                reading = readings.get(f"{ac}_{comms_member.key}")
                if reading is None or not reading.ok:
                    continue
                active = is_binary_active(reading.value)
                if active is not None:
                    # The object reports a fault when active.
                    groups[ac].communicating = not active

        discovery = Discovery(
            groups=groups,
            absent=tuple(
                ac for ac, group in groups.items() if not group.present
            ),
            dead=tuple(
                ac
                for ac, group in groups.items()
                if group.present and group.communicating is False
            ),
            object_count=object_count,
            unrecognised=unrecognised,
            discovered_by=how,
        )
        self.discovery = discovery

        if discovery.dead:
            _LOGGER.warning(
                "%s: %d group(s) publish objects but report a DIII-NET "
                "communication fault and have been skipped: %s",
                self.gateway.host,
                len(discovery.dead),
                ", ".join(address_label(ac) for ac in discovery.dead),
            )
        if not discovery.live_numbers:
            _LOGGER.warning(
                "%s answered but published no live indoor-unit groups",
                self.gateway.host,
            )
        return discovery

    # --- the points we care about -------------------------------------------

    @property
    def active_groups(self) -> tuple[int, ...]:
        """Live groups, narrowed by the user's selection if there is one."""
        live = self.discovery.live_numbers
        selected = self.selected_groups
        if selected is None:
            return live
        chosen = set(selected)
        return tuple(ac for ac in live if ac in chosen)

    def points(self) -> list[PointRef]:
        """Every point a sweep should read.

        Narrowed to what each group actually publishes, so we never spend a
        read on an object the gateway will answer unknown-object for, and
        never create an entity that could only ever be unavailable.
        """
        keys = self.enabled_member_keys
        points: list[PointRef] = []
        for ac in self.active_groups:
            points.extend(self.points_of(ac, keys))
        return points

    def points_of(
        self, ac_number: int, member_keys: tuple[str, ...] | None = None
    ) -> list[PointRef]:
        """One group's points, limited to the members it publishes."""
        keys = self.enabled_member_keys if member_keys is None else member_keys
        group = self.discovery.groups.get(ac_number)
        return [
            point
            for point in points_for(ac_number, keys)
            if group is None or group.publishes(point.member.number)
        ]

    # --- the sweep -----------------------------------------------------------

    #: Sweeps in which nothing at all answered before the BACnet
    #: application is rebuilt. More than one, so a single bad sweep is not
    #: treated as a wedged socket; few enough that recovery is minutes.
    DEAD_SWEEPS_BEFORE_RECONNECT = 3

    async def _async_update_data(self) -> dict[str, PointValue]:
        if not self.gateway.connected:
            raise UpdateFailed(f"{self.gateway.host} is not connected")

        await self._async_check_for_restart()

        points = self.points()
        if not points:
            return {}

        try:
            readings = await self.gateway.read_points(points)
        except DbacsNotReady as err:
            raise UpdateFailed(
                f"{self.gateway.host} is scanning DIII-NET"
            ) from err
        except DbacsError as err:
            raise UpdateFailed(str(err)) from err

        # A per-point failure is returned, not raised, because one dead unit
        # among ninety-six must not fail the sweep. But if *nothing*
        # answered, the sweep has not succeeded in any useful sense, and
        # treating it as success is what let a gateway sit unavailable for
        # two hours behind a config entry still reporting `loaded`, with
        # nothing in the log.
        if not any(reading.ok for reading in readings.values()):
            self._dead_sweeps += 1
            if self._dead_sweeps >= self.DEAD_SWEEPS_BEFORE_RECONNECT:
                self._dead_sweeps = 0
                try:
                    await self.gateway.reconnect()
                except DbacsError as err:
                    _LOGGER.warning(
                        "%s: reconnecting did not help: %s",
                        self.gateway.host, err,
                    )
                else:
                    await self.async_setup_cov()
            raise UpdateFailed(
                f"{self.gateway.host}: none of {len(points)} points "
                f"answered ({self._dead_sweeps} sweep(s) in a row). The "
                "gateway may be unreachable, or this end may need a new "
                "socket."
            )
        self._dead_sweeps = 0

        # Keep whatever change-of-value has told us since the sweep started,
        # rather than letting an older reading overwrite a newer one.
        merged = dict(self.data or {})
        merged.update(readings)
        await self._async_refresh_descriptions(merged)
        self._apply_comms(merged)
        return merged

    def _apply_comms(self, readings: dict[str, PointValue]) -> None:
        """Fold communication status back into what we think of each group."""
        for ac in self.active_groups:
            reading = readings.get(f"{ac}_communication_status")
            if reading is None or not reading.ok:
                continue
            active = is_binary_active(reading.value)
            if active is None:
                continue
            group = self.discovery.groups.get(ac)
            if group is not None:
                group.communicating = not active

    async def _async_refresh_descriptions(
        self, readings: dict[str, PointValue]
    ) -> None:
        """Fetch the two-character fault code for anything in malfunction.

        The malfunction code object's value is 1-512 against a table Daikin
        publishes separately; the code itself (``U1``, ``E7``) is in the
        object's Description property. So a fault is two reads, and we only
        spend the second on units that are actually in alarm.

        Whether a unit is faulted is taken from the Alarm object, which is
        unambiguously Normal or Malfunction, rather than inferred from the
        malfunction code's value. The code is a fallback for when alarm is
        not among the enabled points.
        """
        for ac in self.active_groups:
            code_reading = readings.get(f"{ac}_malfunction_code")
            if code_reading is None or not code_reading.ok:
                continue

            faulted = _faulted(readings.get(f"{ac}_alarm"))
            if faulted is None:
                try:
                    faulted = int(code_reading.value) > 1
                except (TypeError, ValueError):
                    continue

            if not faulted:
                self._descriptions.pop(str(ac), None)
                continue
            try:
                self._descriptions[str(ac)] = (
                    await self.gateway.read_description(code_reading.point)
                )
            except DbacsError as err:
                _LOGGER.debug(
                    "could not read the fault code for %s: %s",
                    address_label(ac),
                    err,
                )

    def rollup(self, name: str):
        """What one refrigerant system's indoor units add up to."""
        from .systems import roll_up  # noqa: PLC0415 - avoids a cycle

        system = self.systems.get(name)
        if system is None:
            return None
        return roll_up(
            system,
            self.data or {},
            set(self.active_groups),
            {ac: self.fault_code(ac) for ac in system.ac_numbers},
        )

    def fault_code(self, ac_number: int) -> str | None:
        """The Daikin two-character code for a group, if it is faulted."""
        return self._descriptions.get(str(ac_number))

    async def _async_check_for_restart(self) -> None:
        """Rebuild subscriptions if the gateway has been power cycled.

        The gateway forgets every change-of-value subscription when it loses
        power and says nothing about it, so the only way to notice is to
        watch something that changes across a restart.
        """
        try:
            info = await self.gateway.read_info()
        except DbacsError:
            return
        if info.restart_token == self._restart_token:
            self.info = info
            return

        _LOGGER.info(
            "%s appears to have restarted; rebuilding change-of-value "
            "subscriptions",
            self.gateway.host,
        )
        self.info = info
        self._restart_token = info.restart_token
        await self.gateway.unsubscribe_all()
        self._cov_points.clear()
        self._cov_failed.clear()
        await self.async_setup_cov()

    # --- change of value -----------------------------------------------------

    async def async_setup_cov(self) -> None:
        """Subscribe the few points worth watching live.

        Four per group, not everything: the gateway allows five recipients
        per object and is shared with a building management head-end, so a
        subscription is a resource to spend deliberately rather than a free
        upgrade.
        """
        if not self.use_cov:
            return

        enabled = set(self.enabled_member_keys)
        wanted = [key for key in COV_MEMBERS if key in enabled]
        if not wanted:
            return

        # Worked out in full before anything is subscribed, because the
        # lifetimes depend on how many there are. See _cov_lifetime.
        planned: list[PointRef] = []
        for ac in self.active_groups:
            group = self.discovery.groups.get(ac)
            for key in wanted:
                member = MEMBERS_BY_KEY[key]
                if group is not None and not group.publishes(member.number):
                    continue
                point = PointRef(ac, member)
                if point.key in self._cov_points:
                    continue
                planned.append(point)

        accepted = 0
        for index, point in enumerate(planned):
            lifetime = self._cov_lifetime(index, len(planned))
            try:
                ok = await self.gateway.subscribe(
                    point, self._cov_update, lifetime
                )
            except DbacsError as err:
                _LOGGER.debug("subscription refused for %s: %s", point.key, err)
                ok = False
            if ok:
                self._cov_points[point.key] = point
                accepted += 1
            else:
                self._cov_failed.add(point.key)

        if self._cov_failed:
            _LOGGER.info(
                "%s accepted %d change-of-value subscription(s); %d were "
                "refused and those points will arrive on the sweep instead",
                self.gateway.host,
                accepted,
                len(self._cov_failed),
            )

    def _cov_lifetime(self, index: int, total: int) -> int:
        """The lifetime for one subscription, counting down from the option.

        bacpypes3 renews a subscription two seconds before its lifetime runs
        out, and every subscription here is made within a second or two of
        the others at startup. One shared lifetime therefore means every
        renewal lands in the same instant: on this site 384 confirmed
        requests at once to a gateway with a 1024-byte APDU, which stalled
        the event loop about half an hour after each start and had the
        Supervisor watchdog restarting Home Assistant every thirty-eight
        minutes.

        Each subscription gets its own lifetime instead, so the renewals are
        spread on the first cycle and stay spread on the ones after, the
        periods now differing as well as the phases. The configured value is
        the longest used, so nothing outlives what was asked for.
        """
        lifetime = self.cov_lifetime
        if total < 2:
            return lifetime
        headroom = max(0, lifetime - COV_MIN_LIFETIME)
        spread = min(headroom, max(total - 1, COV_REFRESH_SPREAD))
        return lifetime - (index * spread) // (total - 1)

    @callback
    def _cov_update(self, point: PointRef, value: Any) -> None:
        """A notification arrived. Fold it in and tell the entities."""
        data = dict(self.data or {})
        data[point.key] = PointValue(point, value=value)
        self.async_set_updated_data(data)

    @property
    def cov_summary(self) -> dict[str, int]:
        return {
            "subscribed": len(self._cov_points),
            "refused": len(self._cov_failed),
        }

    # --- writes --------------------------------------------------------------

    async def async_write(self, point: PointRef, value: Any) -> None:
        """Write one point, if everything agrees that we should.

        Four gates, in order of how badly each one would go wrong: control
        has to be switched on for this entry at all; the write-rate guard has
        to allow it; no other BACnet client may be holding the priority we
        write at; and only then does anything leave the machine.
        """
        if not self.control_enabled:
            raise WriteRefused(
                "Control is switched off for this gateway. Turn it on in the "
                "integration's options if you intend Home Assistant to "
                "command the air conditioning."
            )

        now = self.hass.loop.time()

        # The daily cap is a refusal; the spacing between writes is not.
        # Somebody nudging a setpoint twice should not see an error, so a
        # write that arrives too soon replaces the one already waiting and
        # only the last value is sent.
        budget = self.guard.budget(point.key)
        if (
            budget.last_at is not None
            and now - budget.last_at < self.guard.min_interval
        ):
            if not self.guard.allows_within_cap(point.key, now):
                self.guard.check(point.key, now)  # raises with the reason
            self._coalesce(point, value, budget.last_at)
            self._show_optimistically(point, value)
            return

        self.guard.check(point.key, now)
        await self._async_check_priority(point)
        await self._async_send(point, value)
        self._show_optimistically(point, value)

    async def _async_send(self, point: PointRef, value: Any) -> None:
        """Actually write, and record that we did."""
        try:
            await self.gateway.write_point(
                point, value, priority=self.write_priority
            )
        except DbacsError as err:
            raise WriteRefused(f"{point.object_name}: {err}") from err

        self.guard.record(point.key, self.hass.loop.time())
        self._held[point.key] = (point, self.write_priority)
        _LOGGER.debug(
            "wrote %r to %s at priority %d",
            value,
            point.object_name,
            self.write_priority,
        )

    def _show_optimistically(self, point: PointRef, value: Any) -> None:
        """Show the value now, and let the gateway confirm it.

        The status object is the truth and arrives on the next sweep or by
        change-of-value. This only stops a control springing back under the
        user's finger.
        """
        data = dict(self.data or {})
        data[point.key] = PointValue(point, value=value)
        self.async_set_updated_data(data)

    def _coalesce(
        self, point: PointRef, value: Any, last_at: float
    ) -> None:
        """Hold a write until this point is allowed another one."""
        self._pending[point.key] = (point, value)
        if point.key in self._flushing:
            return  # a flush is already waiting; it will pick up the newest

        delay = max(self.guard.min_interval - (self.hass.loop.time() - last_at), 0)
        self._flushing.add(point.key)
        _LOGGER.debug(
            "holding a write to %s for %.0fs, so writes to one point stay "
            "%.0fs apart",
            point.object_name,
            delay,
            self.guard.min_interval,
        )
        self.entry.async_create_background_task(
            self.hass,
            self._async_flush(point.key, delay),
            name=f"{DOMAIN} flush {point.key}",
        )

    async def _async_flush(self, key: str, delay: float) -> None:
        """Send whatever the newest pending value for a point turned out to be."""
        try:
            await asyncio.sleep(delay)
            pending = self._pending.pop(key, None)
            if pending is None:
                return
            point, value = pending
            try:
                await self._async_check_priority(point)
                await self._async_send(point, value)
            except WriteRefused as err:
                _LOGGER.warning("held write to %s was not sent: %s", key, err)
        finally:
            self._flushing.discard(key)

    @property
    def pending_writes(self) -> dict[str, Any]:
        return {key: value for key, (_point, value) in self._pending.items()}

    async def _async_check_priority(self, point: PointRef) -> None:
        """Refuse to write into a priority slot somebody else is holding.

        The gateway is normally shared with a building management head-end,
        and a slot we do not occupy is one we must not take: overriding the
        building's own control would look entirely normal from the BMS while
        its schedule stopped working.
        """
        held = self._priorities.get(point.key)
        if held is None:
            try:
                held = await self.gateway.read_priority_array(point)
            except DbacsError as err:
                _LOGGER.debug(
                    "could not read the priority array of %s: %s",
                    point.object_name,
                    err,
                )
                held = []
            self._priorities[point.key] = held

        ours = self.write_priority
        contended = [slot for slot in held if slot != ours]
        if ours in held:
            return  # our own earlier write
        if contended:
            raise WriteRefused(
                f"{point.object_name} is being held at BACnet priority "
                f"{', '.join(str(s) for s in contended)} by another client — "
                "most likely the building management system. Home Assistant "
                f"writes at priority {ours} and will not override it. Release "
                "it at the source, or set a different priority in the "
                "integration's options if you know what else is writing."
            )

    def forget_priorities(self) -> None:
        """Re-survey priorities on the next write."""
        self._priorities.clear()

    async def async_release(self, ac_number: int | None = None) -> list[str]:
        """Let go of every priority we are holding, or one group's.

        Returns what was released. A point we never wrote is not ours to
        relinquish, so it is left alone: writing Null at a priority we do not
        hold would be a write in its own right, and on a shared gateway that
        is somebody else's slot to clear.
        """
        if not self._held:
            return []

        released: list[str] = []
        for key, (point, priority) in list(self._held.items()):
            if ac_number is not None and point.ac_number != ac_number:
                continue
            try:
                await self.gateway.relinquish_point(point, priority)
            except DbacsError as err:
                _LOGGER.warning(
                    "could not release %s at priority %d: %s",
                    point.object_name,
                    priority,
                    err,
                )
                continue
            self._held.pop(key, None)
            self._priorities.pop(key, None)
            released.append(point.object_name)

        if released:
            _LOGGER.info(
                "released %d point(s) back to the gateway: %s",
                len(released),
                ", ".join(released),
            )
        # Whatever the unit falls back to is now the truth, and only the
        # gateway knows what that is.
        await self.async_request_refresh()
        return released

    @property
    def held_points(self) -> dict[str, int]:
        return {key: priority for key, (_p, priority) in self._held.items()}

    @property
    def write_summary(self) -> dict[str, Any]:
        now = self.hass.loop.time()
        return {
            "control_enabled": self.control_enabled,
            "priority": self.write_priority,
            "min_interval": self.guard.min_interval,
            "daily_cap": self.guard.daily_cap,
            "cap_enforced": self.guard.enforce_cap,
            "written_last_24h": self.guard.summary(now),
            "pending": self.pending_writes,
            "over_half_of_allowance": self.guard.over_half,
            "held_by_us": self.held_points,
            "priorities_held_elsewhere": {
                key: held for key, held in self._priorities.items() if held
            },
        }

    async def async_shutdown_gateway(self) -> None:
        """Give the gateway its subscription slots back and close the port."""
        await self.gateway.close()


def _faulted(reading: PointValue | None) -> bool | None:
    """Whether an Alarm reading says malfunction, or None if we cannot tell."""
    if reading is None or not reading.ok:
        return None
    return is_binary_active(reading.value)
