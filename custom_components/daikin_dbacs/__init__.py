"""The Daikin D-BACS integration.

Reads a Daikin "Interface for use in BACnet" gateway over BACnet/IP and
turns each DIII-NET indoor-unit group into a Home Assistant device.

Phase 1 is read-only by decision, not by omission. The gateway is normally
shared with a building management head-end, and writing into an occupied
building is a separate piece of work with its own review — see PRD.md.
"""

from __future__ import annotations

import logging
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
from .mapping import DEFAULT_PATH, MappingError, load as load_mapping, plan
from .objects import SELECTABLE, address_label
from .gateway import DbacsError, DbacsGateway

_LOGGER = logging.getLogger(__name__)

#: Every platform is forwarded. The control ones create nothing while
#: control is switched off, which is cheaper and less surprising than
#: changing the platform list underneath a running entry.
SERVICE_RELEASE_CONTROL = "release_control"
SERVICE_APPLY_MAPPING = "apply_mapping"
SERVICE_GENERATE_CARDS = "generate_cards"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.EVENT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type DbacsConfigEntry = ConfigEntry[DbacsCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DbacsConfigEntry) -> bool:
    """Connect to one gateway, discover its groups and start sweeping."""
    gateway = DbacsGateway(
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
        device_instance=entry.data.get(CONF_DEVICE_INSTANCE),
        local_address=entry.data.get(CONF_LOCAL_ADDRESS),
        local_port=entry.data.get(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT),
        our_instance=entry.data.get(CONF_OUR_INSTANCE, DEFAULT_OUR_INSTANCE),
    )

    try:
        info = await gateway.connect()
    except DbacsError as err:
        raise ConfigEntryNotReady(str(err)) from err

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
        site = await hass.async_add_executor_job(load_mapping)
    except MappingError as err:
        _LOGGER.debug("no site mapping, so no outdoor units: %s", err)
    else:
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
        path = call.data.get("path") or DEFAULT_PATH
        try:
            mapping = await hass.async_add_executor_job(load_mapping, path)
        except MappingError as err:
            raise HomeAssistantError(str(err)) from err

        devices = dr.async_get(hass)
        areas = ar.async_get(hass)
        existing = {
            area.name: area.id for area in areas.async_list_areas()
        }

        report: dict[str, Any] = {"gateways": []}
        for entry in hass.config_entries.async_entries(DOMAIN):
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is None:
                continue

            live: dict[int, Any] = {}
            for ac_number in coordinator.active_groups:
                device = devices.async_get_device(
                    identifiers={(DOMAIN, f"{entry.entry_id}_{ac_number}")}
                )
                if device is not None:
                    live[ac_number] = device

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
            for ac_number in coordinator.active_groups:
                device = devices.async_get_device(
                    identifiers={(DOMAIN, f"{entry.entry_id}_{ac_number}")}
                )
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
                cards.append(
                    generated(
                        device_name=name,
                        area_id=device.area_id,
                        faceplate=call.data.get("faceplate"),
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
