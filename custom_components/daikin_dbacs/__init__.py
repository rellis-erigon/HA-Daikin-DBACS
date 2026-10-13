"""The Daikin D-BACS integration.

Reads a Daikin "Interface for use in BACnet" gateway over BACnet/IP and
turns each DIII-NET indoor-unit group into a Home Assistant device.

Phase 1 is read-only by decision, not by omission. The gateway is normally
shared with a building management head-end, and writing into an occupied
building is a separate piece of work with its own review — see PRD.md.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
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
from .objects import SELECTABLE
from .gateway import DbacsError, DbacsGateway

_LOGGER = logging.getLogger(__name__)

#: Every platform is forwarded. The control ones create nothing while
#: control is switched off, which is cheaper and less surprising than
#: changing the platform list underneath a running entry.
SERVICE_RELEASE_CONTROL = "release_control"

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

    hass.services.async_register(
        DOMAIN, SERVICE_RELEASE_CONTROL, release_control
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
