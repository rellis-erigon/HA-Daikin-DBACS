"""Diagnostics: what was found, what was skipped, and why."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import DbacsConfigEntry
from .const import CONF_HOST
from .objects import address_label


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DbacsConfigEntry
) -> dict[str, Any]:
    """Everything needed to tell a quiet gateway from an empty one.

    The host is included because it is the one thing a reader needs to
    correlate this with a network capture, and a diagnostics download is
    something the user chooses to share.
    """
    coordinator = entry.runtime_data
    discovery = coordinator.discovery
    readings = coordinator.data or {}

    errors: dict[str, str] = {
        key: reading.error
        for key, reading in readings.items()
        if reading.error
    }

    return {
        "gateway": {
            "host": entry.data.get(CONF_HOST),
            "device_instance": coordinator.info.device_instance,
            "object_name": coordinator.info.object_name,
            "vendor_name": coordinator.info.vendor_name,
            "model_name": coordinator.info.model_name,
            "firmware_revision": coordinator.info.firmware_revision,
            "application_software_version": (
                coordinator.info.application_software_version
            ),
            "system_status": coordinator.info.system_status,
            "database_revision": coordinator.info.database_revision,
            "protocol_revision": coordinator.info.protocol_revision,
            "max_apdu_length": coordinator.info.max_apdu_length,
            "segmentation_supported": coordinator.info.segmentation_supported,
            "object_count": discovery.object_count,
        },
        "discovery": {
            "method": discovery.discovered_by,
            "live": [address_label(ac) for ac in discovery.live_numbers],
            "live_count": len(discovery.live_numbers),
            "absent_count": len(discovery.absent),
            "dead": [address_label(ac) for ac in discovery.dead],
            "member_sets": _member_sets(coordinator),
            "unrecognised_objects": dict(discovery.unrecognised),
            "note": (
                "Absent groups returned unknown-object: the gateway has no "
                "such point. Dead groups publish points but report a "
                "DIII-NET communication fault."
            ),
        },
        "options": {
            "members": list(coordinator.enabled_member_keys),
            "groups": coordinator.selected_groups,
            "scan_interval": coordinator.scan_interval,
            "use_cov": coordinator.use_cov,
            "cov_lifetime": coordinator.cov_lifetime,
        },
        "change_of_value": coordinator.cov_summary,
        "points": {
            "requested": len(coordinator.points()),
            "answered": sum(1 for r in readings.values() if r.ok),
            "errored": len(errors),
            "errors": dict(sorted(errors.items())[:50]),
        },
        "fault_codes": {
            address_label(ac): coordinator.fault_code(ac)
            for ac in coordinator.active_groups
            if coordinator.fault_code(ac)
        },
    }


def _member_sets(coordinator) -> list[dict[str, Any]]:
    """How many groups publish which set of members.

    Worth reporting because they differ: a system can have two groups
    carrying Forced stop while the rest do not, and firmware decides whether
    the ventilation members exist at all. A support question about a missing
    entity is usually this.
    """
    tally: dict[tuple[int, ...], list[int]] = {}
    for ac_number in coordinator.discovery.live_numbers:
        members = coordinator.discovery.members_of(ac_number)
        key = tuple(sorted(members)) if members is not None else ()
        tally.setdefault(key, []).append(ac_number)
    return [
        {
            "members": list(members) or "unknown (found by probing)",
            "member_count": len(members),
            "group_count": len(groups),
            "example": address_label(groups[0]),
        }
        for members, groups in sorted(
            tally.items(), key=lambda item: -len(item[1])
        )
    ]
