"""The Daikin D-BACS integration.

Reads a Daikin "Interface for use in BACnet" gateway over BACnet/IP and
turns each DIII-NET indoor-unit group into a Home Assistant device.

Phase 1 is read-only by decision, not by omission. The gateway is normally
shared with a building management head-end, and writing into an occupied
building is a separate piece of work with its own review — see PRD.md.
"""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_DEVICE_INSTANCE,
    CONF_HOST,
    CONF_LOCAL_ADDRESS,
    CONF_LOCAL_PORT,
    CONF_OUR_INSTANCE,
    CONF_PORT,
    DEFAULT_LOCAL_PORT,
    DEFAULT_OUR_INSTANCE,
    DEFAULT_PORT,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from .coordinator import DbacsCoordinator
from .cards import generated
from .systems import systems_from_mapping
from .mapping import MappingError, load as load_mapping, path_for, plan
from .objects import (
    MEMBERS,
    MEMBERS_BY_KEY,
    PROBE_MEMBER,
    SELECTABLE,
    PointRef,
    address_label,
)
from .gateway import DbacsError, DbacsGateway

_LOGGER = logging.getLogger(__name__)

#: Every platform is forwarded. The control ones create nothing while
#: control is switched off, which is cheaper and less surprising than
#: changing the platform list underneath a running entry.
SERVICE_RELEASE_CONTROL = "release_control"
SERVICE_APPLY_MAPPING = "apply_mapping"
SERVICE_GENERATE_CARDS = "generate_cards"
SERVICE_PURGE_ORPHANS = "purge_orphans"
SERVICE_RECHECK = "recheck"
SERVICE_SELF_TEST = "self_test"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.EVENT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type DbacsConfigEntry = ConfigEntry[DbacsCoordinator]


def _group_devices(registry, entry) -> dict[int, Any]:
    """This entry's indoor-unit devices, keyed by DIII AC number.

    Built from the registry's own list for the config entry rather than
    looked up one identifier at a time. `DeviceRegistry` has no public
    single-identifier lookup, and calling one that does not exist returned
    nothing for every group without raising — which left `apply_mapping`
    reporting no matches and `generate_cards` skipping all 223 units with
    "has no device". The test stub implemented the invented method, so the
    suite could not see it.

    Only the group devices come back: the gateway's own device has no
    numeric suffix, and the outdoor units' identifiers end in a system name.
    """
    prefix = f"{entry.entry_id}_"
    found: dict[int, Any] = {}
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain != DOMAIN or not identifier.startswith(prefix):
                continue
            tail = identifier[len(prefix):]
            if tail.isdigit():
                found[int(tail)] = device
    return found


#: BACnet reserves 4194303 for a device that has not been given an instance.
WILDCARD_INSTANCE = 4194303


async def _try_reads(gateway, coordinator, batches) -> dict[str, Any]:
    """Read increasing batches of points, reporting what answered."""
    member = MEMBERS[PROBE_MEMBER]
    groups = list(coordinator.active_groups)
    requested = answered = 0
    timings: dict[str, Any] = {}
    for size in batches:
        points = [PointRef(ac, member) for ac in groups[:size]]
        if not points:
            continue
        started = time.monotonic()
        try:
            readings = await gateway.read_points(points)
        except DbacsError as err:
            timings[str(size)] = f"failed: {err}"
            requested += len(points)
            continue
        good = sum(1 for r in readings.values() if r and r.ok)
        requested += len(points)
        answered += good
        timings[str(size)] = (
            f"{good}/{len(points)} in {time.monotonic() - started:.1f}s"
        )
    return {
        "requested": requested,
        "answered": answered,
        "seconds": timings,
    }


def _local_identity(hass: HomeAssistant, entry) -> tuple[int, int]:
    """The UDP port and BACnet device instance this entry should use locally.

    One gateway can keep the defaults. A second cannot share them: two
    bacpypes3 applications bound to the same port in one process means
    replies go to whichever socket the operating system hands them to, and
    two local devices claiming one instance number is a protocol violation
    besides. On this site the result was gateway 11 receiving everything
    while all 2,016 of gateway 12's points timed out — with the config entry
    still reporting `loaded`, so the only symptom was half the estate
    reading `unavailable`.

    Uniqueness is enforced rather than merely defaulted. The config flow
    writes both values into every entry, so there is no way to tell a
    deliberate 47808 from an accepted default — and a deliberate collision
    cannot work either. Entries earlier in the list keep what they have and
    any later one that clashes is moved, which is stable across restarts
    because the order is creation order and the result is written back.
    """
    others = [
        other for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ]
    position = {
        e.entry_id: i
        for i, e in enumerate(hass.config_entries.async_entries(DOMAIN))
    }
    mine = position.get(entry.entry_id, len(position))
    earlier = [o for o in others if position.get(o.entry_id, -1) < mine]

    taken_ports = {
        int(o.data.get(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT)) for o in earlier
    }
    taken_instances = {
        int(o.data.get(CONF_OUR_INSTANCE, DEFAULT_OUR_INSTANCE))
        for o in earlier
    }

    port = int(entry.data.get(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT))
    while port in taken_ports:
        port += 1

    instance = int(entry.data.get(CONF_OUR_INSTANCE, DEFAULT_OUR_INSTANCE))
    while instance in taken_instances or instance >= WILDCARD_INSTANCE:
        instance -= 1

    return port, instance


async def async_setup_entry(hass: HomeAssistant, entry: DbacsConfigEntry) -> bool:
    """Connect to one gateway, discover its groups and start sweeping."""
    local_port, our_instance = _local_identity(hass, entry)
    if (entry.data.get(CONF_LOCAL_PORT) != local_port
            or entry.data.get(CONF_OUR_INSTANCE) != our_instance):
        _LOGGER.info(
            "%s will use local port %s and BACnet instance %s, so that it "
            "does not share a socket or an identity with another gateway",
            entry.data[CONF_HOST], local_port, our_instance,
        )
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_LOCAL_PORT: local_port,
                  CONF_OUR_INSTANCE: our_instance},
        )

    gateway = DbacsGateway(
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
        device_instance=entry.data.get(CONF_DEVICE_INSTANCE),
        local_address=entry.data.get(CONF_LOCAL_ADDRESS),
        local_port=local_port,
        our_instance=our_instance,
    )

    try:
        info = await gateway.connect()
    except DbacsError as err:
        raise ConfigEntryNotReady(str(err)) from err

    # Record the instance the gateway actually reported, if the entry has a
    # different one or none. The mapping file is named after it, so an entry
    # written before this was checked — the second gateway here recorded 0 —
    # repairs itself on the next reload instead of needing to be re-added
    # and losing anything set by hand.
    if entry.data.get(CONF_DEVICE_INSTANCE) != info.device_instance:
        _LOGGER.info(
            "%s reports BACnet device instance %s; the entry said %s, so "
            "recording the gateway's own answer",
            entry.data[CONF_HOST],
            info.device_instance,
            entry.data.get(CONF_DEVICE_INSTANCE),
        )
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_DEVICE_INSTANCE: info.device_instance},
        )

    coordinator = DbacsCoordinator(hass, entry, gateway, info)

    try:
        await coordinator.async_discover()
    except DbacsError as err:
        await gateway.close()
        raise ConfigEntryNotReady(str(err)) from err

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await gateway.close()
        raise

    await coordinator.async_setup_cov()

    # The gateway itself is a device, so the 96 indoor-unit groups have
    # something real to hang off. Registering it here rather than implying it
    # from an entity means `via_device_id` points at a device that exists.
    registry = dr.async_get(hass)
    hub = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=info.object_name or f"D-BACS {entry.data[CONF_HOST]}",
        manufacturer=info.vendor_name or MANUFACTURER,
        model=info.model_name or MODEL,
        sw_version=info.application_software_version or info.firmware_revision,
    )
    coordinator.hub_device_id = hub.id

    # Refrigerant systems, if the site has told us which units share one.
    # The gateway cannot: its only outdoor information is a compressor flag
    # on each indoor unit.
    try:
        site_path = path_for(info.device_instance)
        site = await hass.async_add_executor_job(load_mapping, site_path)
    except MappingError as err:
        _LOGGER.debug("no site mapping, so no outdoor units: %s", err)
    else:
        coordinator.site = site
        coordinator.systems = systems_from_mapping(site)
        if coordinator.systems:
            _LOGGER.debug(
                "%d refrigerant system(s) from the site mapping",
                len(coordinator.systems),
            )

    entry.runtime_data = coordinator
    _reconcile_enabled_entities(hass, entry, coordinator)
    _register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _register_services(hass: HomeAssistant) -> None:
    """Register the services, once, however many gateways are set up."""
    if hass.services.has_service(DOMAIN, SERVICE_RELEASE_CONTROL):
        return

    async def release_control(call) -> None:
        device_id = call.data.get("device_id")
        wanted: tuple[str, int] | None = None
        if device_id:
            device = dr.async_get(hass).async_get(device_id)
            if device is None:
                raise HomeAssistantError(f"no such device: {device_id}")
            wanted = _entry_and_group_of(device)
            if wanted is None:
                raise HomeAssistantError(
                    "that device is not a Daikin D-BACS indoor-unit group"
                )

        released: list[str] = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is None:
                continue
            if wanted is not None and wanted[0] != entry.entry_id:
                continue
            released += await coordinator.async_release(
                None if wanted is None else wanted[1]
            )
        _LOGGER.info("release_control released %d point(s)", len(released))

    async def apply_mapping(call) -> dict[str, Any]:
        """Name and place the discovered units from a site mapping file.

        Reports by default. A dry run is the same code path as the real
        thing — the plan is computed, then applied or not — so what you are
        shown is what would happen rather than a description of it.
        """
        dry_run = call.data.get("dry_run", True)
        override = call.data.get("path")
        wanted_host = call.data.get("host")

        targets = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if getattr(entry, "runtime_data", None) is not None
            and (not wanted_host or entry.data.get(CONF_HOST) == wanted_host)
        ]
        if wanted_host and not targets:
            raise HomeAssistantError(
                f"no configured gateway at {wanted_host}"
            )
        # An explicit file names one gateway's units. Spreading it across
        # several would rename each gateway after the first one's rooms —
        # plausibly, and without any error to notice.
        if override and len(targets) > 1:
            raise HomeAssistantError(
                "path applies to one gateway, but "
                f"{len(targets)} are configured. Name one with host, or "
                "leave path unset so each gateway uses its own file."
            )

        devices = dr.async_get(hass)
        areas = ar.async_get(hass)
        existing = {
            area.name: area.id for area in areas.async_list_areas()
        }

        report: dict[str, Any] = {"gateways": []}
        for entry in targets:
            coordinator = entry.runtime_data
            path = override or path_for(entry.data.get(CONF_DEVICE_INSTANCE))
            try:
                mapping = await hass.async_add_executor_job(load_mapping, path)
            except MappingError as err:
                raise HomeAssistantError(f"{entry.title}: {err}") from err

            known = _group_devices(devices, entry)
            live: dict[int, Any] = {
                ac_number: known[ac_number]
                for ac_number in coordinator.active_groups
                if ac_number in known
            }

            outcome = plan(
                mapping,
                live,
                set_names=call.data.get("set_names", True),
                set_areas=call.data.get("set_areas", True),
                existing_areas=existing,
                overwrite=call.data.get("overwrite", False),
            )

            if not dry_run:
                for change in outcome.changes:
                    device = live.get(change.ac_number)
                    if device is None:
                        continue
                    if change.field == "name":
                        devices.async_update_device(
                            device.id, name_by_user=change.new
                        )
                        change.applied = True
                    elif change.field == "area":
                        area_id = existing.get(change.new)
                        if area_id is None:
                            area_id = areas.async_create(change.new).id
                            existing[change.new] = area_id
                        devices.async_update_device(device.id, area_id=area_id)
                        change.applied = True

            summary = outcome.summary()
            summary["gateway"] = entry.title
            summary["dry_run"] = bool(dry_run)
            summary["mapping_file"] = path
            report["gateways"].append(summary)
            _LOGGER.info(
                "apply_mapping on %s: %d change(s), %s",
                entry.title,
                len(outcome.changes),
                "reported only" if dry_run else "applied",
            )

        return report

    hass.services.async_register(
        DOMAIN, SERVICE_RELEASE_CONTROL, release_control
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_MAPPING,
        apply_mapping,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def generate_cards(call) -> dict[str, Any]:
        """A card for every live group, for the bridge-devices strategy.

        Returns the shape that strategy consumes — cards keyed by device and
        area — plus whatever was skipped and why, because a unit missing
        from a dashboard with no explanation is worse than one that says it
        has no entities yet.
        """
        wanted_area = call.data.get("area")
        devices = dr.async_get(hass)
        entities = er.async_get(hass)

        cards: list[dict[str, Any]] = []
        skipped: list[str] = []

        for entry in hass.config_entries.async_entries(DOMAIN):
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is None:
                continue
            known = _group_devices(devices, entry)
            for ac_number in coordinator.active_groups:
                device = known.get(ac_number)
                if device is None:
                    skipped.append(f"{address_label(ac_number)} has no device")
                    continue
                if wanted_area and device.area_id != wanted_area:
                    continue

                name = device.name_by_user or device.name
                if not name:
                    skipped.append(f"{address_label(ac_number)} has no name")
                    continue

                climate_entity = entities.async_get_entity_id(
                    "climate", DOMAIN, f"{entry.entry_id}_{ac_number}_climate"
                )
                # Precedence: what the site says about this unit, then
                # what the caller asked for, then the default. A site that
                # has deliberately said one unit is a wall controller is
                # not overridden by a blanket dashboard setting.
                entry_site = coordinator.site.get(ac_number)
                faceplate = (
                    getattr(entry_site, "faceplate", None)
                    or call.data.get("faceplate")
                )
                cards.append(
                    generated(
                        device_name=name,
                        area_id=device.area_id,
                        faceplate=faceplate,
                        climate_entity=climate_entity,
                    )
                )

        _LOGGER.debug("generate_cards produced %d card(s)", len(cards))
        return {"cards": cards, "skipped": skipped}

    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_CARDS,
        generate_cards,
        supports_response=SupportsResponse.OPTIONAL,
    )


    async def purge_orphans(call) -> dict[str, Any]:
        """Remove entities for points the gateway does not publish.

        These collect when a gateway is first set up without its member
        map: discovery falls back to probing, which finds the units but not
        which members each publishes, so an entity is made for every member
        this integration models. On a gateway whose firmware predates the
        ventilation members that is two dead entities per group, and they
        can never read anything.

        Once the member map is known they are not created at all, so this
        is only for what is already in the registry. Reports by default.

        Deliberately narrow. An entity is an orphan only when its point
        belongs to a group the gateway does not have, or to a member that
        group does not publish. A point that is published but switched off
        in the options is not an orphan — that is a choice, not a fault —
        and anything whose unique id is not a member point, such as the
        climate entity, the fault event or an outdoor unit, is left alone.
        """
        dry_run = call.data.get("dry_run", True)
        wanted_host = call.data.get("host")
        registry = er.async_get(hass)

        orphans: list[str] = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is None:
                continue
            if wanted_host and entry.data.get(CONF_HOST) != wanted_host:
                continue

            prefix = f"{entry.entry_id}_"
            live = set(coordinator.active_groups)
            for registered in list(
                er.async_entries_for_config_entry(registry, entry.entry_id)
            ):
                unique_id = registered.unique_id or ""
                if not unique_id.startswith(prefix):
                    continue
                tail = unique_id[len(prefix):]
                ac_text, _, key = tail.partition("_")
                if not ac_text.isdigit() or key not in MEMBERS_BY_KEY:
                    # Not a member point: the climate entity, the fault
                    # event, an outdoor unit. Nothing to judge.
                    continue
                ac_number = int(ac_text)
                group = coordinator.discovery.groups.get(ac_number)
                if ac_number in live and group is not None and group.publishes(
                    MEMBERS_BY_KEY[key].number
                ):
                    continue
                orphans.append(registered.entity_id)

        removed = 0
        if not dry_run:
            for entity_id in orphans:
                registry.async_remove(entity_id)
                removed += 1
            if removed:
                _LOGGER.info(
                    "purge_orphans removed %d entit%s for points the "
                    "gateway does not publish",
                    removed,
                    "y" if removed == 1 else "ies",
                )

        return {
            "orphans": orphans,
            "count": len(orphans),
            "removed": removed,
            "dry_run": bool(dry_run),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_PURGE_ORPHANS,
        purge_orphans,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def recheck(call) -> dict[str, Any]:
        """Ask every gateway about its faults now, rather than next sweep."""
        wanted_host = call.data.get("host")
        report: dict[str, Any] = {"gateways": []}
        for entry in hass.config_entries.async_entries(DOMAIN):
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is None:
                continue
            if wanted_host and entry.data.get(CONF_HOST) != wanted_host:
                continue
            try:
                summary = await coordinator.async_recheck()
            except DbacsError as err:
                summary = {"host": entry.data.get(CONF_HOST), "ok": False,
                           "error": str(err)}
            summary["gateway"] = entry.title
            report["gateways"].append(summary)
        return report

    hass.services.async_register(
        DOMAIN,
        SERVICE_RECHECK,
        recheck,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def self_test(call) -> dict[str, Any]:
        """Say whether a silent gateway is the gateway's fault or ours.

        The live connection is tried first: the device object, then batches
        of 1, 4, 16 and 64 points. If it answers, that is the end of it.

        If it does not, a second connection is opened on a spare local port
        and the same reads are tried again. That comparison is the whole
        diagnosis — when this happened for real, a fresh connection read the
        gateway in 0.6 seconds and 58 of 64 points while the integration
        could not read one, which ruled out the gateway, the network and the
        APDU size together. Doing it here means nobody needs a shell on the
        container to find that out.

        Reads only. Nothing is written and no subscription is made.
        """
        wanted_host = call.data.get("host")
        batches = call.data.get("batches") or [1, 4, 16, 64]
        report: dict[str, Any] = {"gateways": []}

        for entry in hass.config_entries.async_entries(DOMAIN):
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is None:
                continue
            host = entry.data.get(CONF_HOST)
            if wanted_host and host != wanted_host:
                continue

            live = await _try_reads(coordinator.gateway, coordinator, batches)
            result: dict[str, Any] = {"host": host, "live": live}

            if live["answered"]:
                result["verdict"] = "working"
                result["advice"] = (
                    f"{host} answered {live['answered']} of "
                    f"{live['requested']} points."
                )
            else:
                fresh_gateway = DbacsGateway(
                    host,
                    entry.data.get(CONF_PORT, DEFAULT_PORT),
                    device_instance=entry.data.get(CONF_DEVICE_INSTANCE),
                    local_address=entry.data.get(CONF_LOCAL_ADDRESS),
                    # A port of 0 lets the operating system pick a free one,
                    # so this never collides with a gateway that is loaded.
                    local_port=0,
                    our_instance=WILDCARD_INSTANCE - 1,
                )
                try:
                    await fresh_gateway.connect()
                except DbacsError as err:
                    fresh = {"requested": 0, "answered": 0,
                             "error": str(err)}
                else:
                    fresh = await _try_reads(fresh_gateway, coordinator,
                                             batches)
                finally:
                    with suppress(Exception):
                        await fresh_gateway.close()

                result["fresh"] = fresh
                if fresh["answered"]:
                    result["verdict"] = "our end"
                    result["advice"] = (
                        f"{host} answered a new connection but not the one "
                        "Home Assistant holds, so the gateway is fine and "
                        "this end needs a new socket. Reload the entry; the "
                        "sweep also does this by itself after "
                        f"{DbacsCoordinator.DEAD_SWEEPS_BEFORE_RECONNECT} "
                        "silent sweeps."
                    )
                else:
                    result["verdict"] = "the gateway"
                    result["advice"] = (
                        f"{host} did not answer a new connection either, so "
                        "the gateway or the network between is at fault "
                        "rather than Home Assistant."
                    )

            _LOGGER.info("self_test on %s: %s — %s", host,
                         result["verdict"], result["advice"])
            report["gateways"].append(result)

        if not report["gateways"]:
            raise HomeAssistantError(
                f"no configured gateway at {wanted_host}" if wanted_host
                else "no gateway is set up"
            )
        return report

    hass.services.async_register(
        DOMAIN,
        SERVICE_SELF_TEST,
        self_test,
        supports_response=SupportsResponse.OPTIONAL,
    )

def _entry_and_group_of(device) -> tuple[str, int] | None:
    """The config entry and AC number a device belongs to."""
    for domain, identifier in device.identifiers:
        if domain != DOMAIN or "_" not in identifier:
            continue
        entry_id, _, ac = identifier.rpartition("_")
        if ac.isdigit():
            return entry_id, int(ac)
    return None


def _reconcile_enabled_entities(
    hass: HomeAssistant, entry: DbacsConfigEntry, coordinator: DbacsCoordinator
) -> None:
    """Make the registry agree with the chosen point list.

    `entity_registry_enabled_default` only applies the first time an entity
    is created. Without this, adding a point in the options left its entities
    disabled from a previous run and they were never read again — so the
    bulk enable and disable this integration advertises did nothing after
    the first setup.

    The chosen list is the authority, and only entities this integration
    disabled are re-enabled: a point somebody switched off by hand stays off.
    """
    registry = er.async_get(hass)
    chosen = set(coordinator.enabled_member_keys)
    governed = {member.key for member in SELECTABLE}
    enabled = disabled = 0

    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        key = _member_key_of(entity.unique_id, entry.entry_id)

        # Only readable points are governed by the chosen list. A thermostat,
        # a fault event, a command switch and a reset button are governed by
        # whether control is on, and a command member's key is never in the
        # chosen list — so without this they were all disabled as if they
        # were points somebody had deselected.
        if key is None or key not in governed:
            if entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
                registry.async_update_entity(entity.entity_id, disabled_by=None)
                enabled += 1
            continue

        wanted = key in chosen

        if wanted and entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            registry.async_update_entity(entity.entity_id, disabled_by=None)
            enabled += 1
        elif not wanted and entity.disabled_by is None:
            registry.async_update_entity(
                entity.entity_id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )
            # Home Assistant has already published a restored state for this
            # entity by the time setup reaches here, and disabling the
            # registry entry does not take that away — it lingers as
            # `unavailable` until the whole of Home Assistant restarts.
            # Narrowing the point list would otherwise leave hundreds of
            # dead entities on the device pages.
            hass.states.async_remove(entity.entity_id)
            disabled += 1

    if enabled or disabled:
        _LOGGER.debug(
            "point selection changed: enabled %d entities, disabled %d",
            enabled,
            disabled,
        )


def _member_key_of(unique_id: str, entry_id: str) -> str | None:
    """The member key out of one of our unique ids, or None if it has none.

    Ours look like `<entry_id>_<ac number>_<member key>`, and the member key
    is the only part that varies by point.
    """
    prefix = f"{entry_id}_"
    if not unique_id.startswith(prefix):
        return None
    rest = unique_id[len(prefix) :]
    _ac, _, key = rest.partition("_")
    if not key or key in ("climate", "fault_event"):
        return None
    return key


async def async_unload_entry(
    hass: HomeAssistant, entry: DbacsConfigEntry
) -> bool:
    """Unload the entry, handing the gateway its resources back."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown_gateway()
    return unloaded


async def _async_options_updated(
    hass: HomeAssistant, entry: DbacsConfigEntry
) -> None:
    """Reload when the enabled points change.

    A point coming or going changes which entities exist, which is a reload
    rather than something to patch in place.
    """
    await hass.config_entries.async_reload(entry.entry_id)
