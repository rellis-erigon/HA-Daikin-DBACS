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
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

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
from .gateway import DbacsError, DbacsGateway

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

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
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


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
