"""Discovery, sweeping and change-of-value for one gateway."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_COV_LIFETIME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OPT_COV_LIFETIME,
    OPT_GROUPS,
    OPT_MEMBERS,
    OPT_SCAN_INTERVAL,
    OPT_USE_COV,
)
from .gateway import (
    DbacsError,
    DbacsGateway,
    DbacsNotReady,
    GatewayInfo,
    PointValue,
)
from .objects import (
    COV_MEMBERS,
    DEFAULT_MEMBERS,
    MAX_AC_NUMBER,
    MEMBERS_BY_KEY,
    PointRef,
    READABLE,
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
        self._restart_token = info.restart_token
        self._cov_points: dict[str, PointRef] = {}
        self._cov_failed: set[str] = set()
        self._descriptions: dict[str, str | None] = {}

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
        known = {member.key for member in READABLE}
        return tuple(key for key in chosen if key in known)

    @property
    def selected_groups(self) -> tuple[int, ...] | None:
        """The groups the user has narrowed to, or None for "all that live"."""
        chosen = self.entry.options.get(OPT_GROUPS)
        if not chosen:
            return None
        return tuple(sorted(int(value) for value in chosen))

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
        found = await self.gateway.with_retry_when_not_ready(
            lambda: self.gateway.probe_groups(ALL_AC_NUMBERS)
        )

        groups = {
            ac: GroupState(ac_number=ac, present=present)
            for ac, present in sorted(found.items())
        }
        present_numbers = [ac for ac, group in groups.items() if group.present]
        _LOGGER.debug(
            "%s publishes %d of %d possible groups",
            self.gateway.host,
            len(present_numbers),
            len(ALL_AC_NUMBERS),
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
            object_count=await self.gateway.read_object_list_count(),
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
        """Every point a sweep should read."""
        keys = self.enabled_member_keys
        points: list[PointRef] = []
        for ac in self.active_groups:
            points.extend(points_for(ac, keys))
        return points

    # --- the sweep -----------------------------------------------------------

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

        accepted = 0
        for ac in self.active_groups:
            for key in wanted:
                point = PointRef(ac, MEMBERS_BY_KEY[key])
                if point.key in self._cov_points:
                    continue
                try:
                    ok = await self.gateway.subscribe(
                        point, self._cov_update, self.cov_lifetime
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

    async def async_shutdown_gateway(self) -> None:
        """Give the gateway its subscription slots back and close the port."""
        await self.gateway.close()


def _faulted(reading: PointValue | None) -> bool | None:
    """Whether an Alarm reading says malfunction, or None if we cannot tell."""
    if reading is None or not reading.ok:
        return None
    return is_binary_active(reading.value)
