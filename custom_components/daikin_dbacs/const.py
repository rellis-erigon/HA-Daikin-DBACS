"""Constants for the Daikin D-BACS integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "daikin_dbacs"

# --- config entry keys -------------------------------------------------------

CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_DEVICE_INSTANCE: Final = "device_instance"
CONF_LOCAL_ADDRESS: Final = "local_address"
CONF_LOCAL_PORT: Final = "local_port"
CONF_OUR_INSTANCE: Final = "our_instance"

# --- options keys ------------------------------------------------------------

OPT_SCAN_INTERVAL: Final = "scan_interval"
OPT_MEMBERS: Final = "members"
OPT_GROUPS: Final = "groups"
OPT_USE_COV: Final = "use_cov"
OPT_COV_LIFETIME: Final = "cov_lifetime"
OPT_CONTROL: Final = "control"
OPT_WRITE_PRIORITY: Final = "write_priority"
OPT_MIN_WRITE_INTERVAL: Final = "min_write_interval"
OPT_DAILY_WRITE_CAP: Final = "daily_write_cap"
OPT_ENFORCE_WRITE_CAP: Final = "enforce_write_cap"
OPT_GATED: Final = "gated"

# --- defaults ----------------------------------------------------------------

DEFAULT_PORT: Final = 47808
DEFAULT_LOCAL_PORT: Final = 47808

#: Our own BACnet device instance. High in the range on purpose: a gateway,
#: a BMS head-end and a second Home Assistant all have to coexist, and the
#: low numbers are where commissioned equipment lives.
DEFAULT_OUR_INSTANCE: Final = 4194300

#: Change-of-value carries the four points that want to be live, so the sweep
#: is a safety net and the source for everything else. Two minutes: setpoint
#: and mode are not subscribed, and a change made at a wall controller should
#: not take five minutes to appear. Measured against a production gateway, a
#: sweep of every member of every group takes about 4.5 s, so this is well
#: under 4% duty cycle on a gateway shared with a head-end.
DEFAULT_SCAN_INTERVAL: Final = 120
MIN_SCAN_INTERVAL: Final = 60

#: Renewed well before it lapses. The gateway drops every subscription on a
#: power cycle without saying so, which the coordinator watches for
#: separately.
DEFAULT_COV_LIFETIME: Final = 1800

#: Subscriptions are given lifetimes counting down from the configured one so
#: that bacpypes3, which refreshes each two seconds before its own lifetime
#: runs out, does not refresh all of them at once. Spread over this many
#: seconds, which at 96 groups puts about six seconds between refreshes.
COV_REFRESH_SPREAD: Final = 600

#: However short the configured lifetime, no subscription is given less than
#: this. A subscription refreshing every few seconds would cost more than the
#: storm the spread exists to prevent.
COV_MIN_LIFETIME: Final = 120

#: Objects per ReadPropertyMultiple. The gateway accepts a 1024-byte APDU
#: and segments, but a modest chunk keeps one slow group from delaying the
#: rest and keeps a failure attributable.
RPM_CHUNK: Final = 8

#: Concurrent requests in flight during a sweep. The gateway is shared with
#: a building management head-end; politeness here is not optional.
MAX_CONCURRENCY: Final = 4

#: Seconds to wait for one request before giving up on it. Comfortably inside
#: the gateway's own 3 s APDU timeout times its retries.
REQUEST_TIMEOUT: Final = 10

#: How long to keep retrying a gateway that answers CONFIGURATION_IN_PROGRESS.
#: It is scanning its DIII-NET ports and will answer properly when it is done.
CONFIG_IN_PROGRESS_RETRIES: Final = 10
CONFIG_IN_PROGRESS_BACKOFF: Final = 6

#: Control is off until it is asked for. Installing an update must not
#: quietly make a building writable, and the write path has a review of its
#: own — see PRD.md.
DEFAULT_CONTROL: Final = False

#: The lowest BACnet priority there is, which is where a dashboard belongs:
#: whatever the building's own control system holds sits above us.
DEFAULT_WRITE_PRIORITY: Final = 16

#: Seconds between writes to the same point. See writes.py for why.
DEFAULT_MIN_WRITE_INTERVAL: Final = 60

#: Setpoint range offered. The unit clamps to its own limits silently, and
#: those depend on the indoor and outdoor models and the current mode, so
#: this is a sane envelope rather than a claim about any one unit.
DEFAULT_MIN_TEMP: Final = 16.0
DEFAULT_MAX_TEMP: Final = 32.0
TEMP_STEP: Final = 0.5

MANUFACTURER: Final = "Daikin"
MODEL: Final = "Interface for use in BACnet"
