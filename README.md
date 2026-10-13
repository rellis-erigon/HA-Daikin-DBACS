# Daikin D-BACS for Home Assistant

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](custom_components/daikin_dbacs/manifest.json)

> Connect a Daikin **Interface for use in BACnet** gateway to Home Assistant,
> and get one device per indoor-unit group — no changes on the air-conditioning
> side.

A native Home Assistant integration that reads a Daikin `DMS502B51` /
`DMS502B71` gateway over **BACnet/IP** and creates entities directly. No
add-on, no MQTT broker, no BACnet broadcast.

---

## How it works

```
┌────────────────────────┐                  ┌─────────────────────────────┐
│ Daikin Interface for   │   BACnet/IP      │ Home Assistant              │
│ use in BACnet          │◄──── unicast ────│  daikin_dbacs               │
│ (DMS502B51 / B71)      │   RPM + COV      │                             │
│                        │                  │  sensor.*                   │
│  DIII-NET ×4 ports     │─── D3 ──────────►│  binary_sensor.*            │
└────────────────────────┘  indoor groups   │  devices                    │
                                            └─────────────────────────────┘
```

1. Add the integration via **Settings → Devices & services → Add integration**
2. Enter the gateway's IP address. The connection is tested before the entry
   is created, and a failure tells you which failure it was.
3. Every DIII-NET indoor-unit group is found automatically, and each becomes
   its own Home Assistant device named after its address — `DBACS 2 · 1-05`.
4. Run status, alarm, malfunction code and room temperature arrive by
   **change-of-value** as they move. Everything else is read on a slow sweep.
5. Choose which points you want in **Configure** — once, applied to every
   group.

## Version 0.1 is read-only, deliberately

Nothing in this release writes to the gateway. That is a decision rather than
an omission.

These gateways usually sit under a building management head-end, and a BACnet
write holds a priority slot until it is released — a point held from Home
Assistant looks normal to the BMS while the building's own control stops
working. Daikin also caps setting changes at **7,000 per year per point**,
which is nineteen a day, and an automation with a bad trigger will spend that
in an afternoon.

Writing is Phase 2, and it arrives with a priority survey and a write-rate
guard rather than on its own. See [PRD.md](PRD.md).

## What you get per indoor unit

| Entity | From |
|---|---|
| Running | `StartStopStatus` |
| Alarm | `Alarm` |
| Malfunction code | `MalfunctionCode`, with the two-character Daikin code |
| Mode | `AirConModeStatus` |
| Fan speed | `AirFlowRateStatus` |
| Room temperature | `RoomTemp` |
| Setpoint | `TempAdjust` |
| Filter | `FilterSign` |
| Thermostat | `ThermoStatus` |
| Communication | `CommunicationStatus` |

Ten points, enabled by default, so a gateway comes up looking complete. A
further twenty-three are available and start disabled — compressor, indoor
fan, heater, air direction, energy saving, forced thermostat, accumulated
power, and the ventilation points for heat reclaim ventilators.

Every entity carries the BACnet object it reads, its instance number and its
DIII-NET address as attributes, so a value can always be traced back to the
object it came from.

## Choosing points, in bulk

A fully expanded gateway publishes **256 groups of 33 readable points** —
nearly nine thousand. Reading all of them is not a thing anyone should do, so
the point list is the load control, and it works on two axes:

- **By point, across every group.** "Give me setpoint on everything." "I
  never want heater status." Chosen once in **Configure** and applied to every
  group, present and future.
- **By group.** Narrow it to switch a floor or a wing off wholesale.

A point you have not chosen still exists in the entity registry, greyed out on
its device page, so Home Assistant's own entity list can bulk-enable it with
checkboxes. **What matters is that a disabled point is never read and never
subscribed** — the sweep is built from enabled entities only.

## Absent, dead, and unavailable

Three different things, kept apart, because running them together is how a
dashboard ends up confidently wrong:

- **Absent** — the gateway answers `unknown-object`. Nothing is wired to that
  slot. No device is created.
- **Dead** — the objects exist but `CommunicationStatus` reports a DIII-NET
  fault. The group is wired and not answering. No device, logged as a warning,
  listed in diagnostics, and picked up automatically if it comes back.
- **Unavailable** — a live point we could not read this time. The entity goes
  unavailable rather than holding its last value, because a confident number
  from a dead point is the one failure worth designing against.

A gateway that is still scanning its DIII-NET ports answers
`configuration-in-progress`. That means *not yet*, not *nothing there*, so it
is waited out rather than treated as an empty system.

## Change-of-value

The gateway accepts **five subscribers per object**, which is per object
rather than per gateway — so an existing head-end's subscriptions do not lock
Home Assistant out. Four points per group are subscribed; the rest are swept.

Two things to know:

- A refused subscription is not an error. That point simply arrives on the
  sweep instead.
- **The gateway forgets every subscription when it loses power**, and does not
  mention it. The integration watches the device object for a restart and
  rebuilds them.

## Installing

Add this repository to HACS as a custom **Integration** repository, install,
and restart Home Assistant. `bacpypes3` is installed automatically from the
manifest.

Or copy `custom_components/daikin_dbacs` into your `custom_components`
directory by hand.

## Network notes

- **Unicast only.** You give the gateway's address, so nothing is discovered
  by broadcast and no BBMD configuration is needed — including across subnets.
- **The local port is configurable.** Home Assistant binds UDP 47808 by
  default; change it if another BACnet client on the same host already has it.
- **Our device instance is configurable**, and must be unique on the network.
  The default is high in the range on purpose, because the low numbers are
  where commissioned equipment lives.
- If a unicast Who-Is goes unanswered you can enter the gateway's **device
  instance number** by hand. It is set with Daikin's Trial Operation tool.

## Things worth knowing about the object model

Written down here because each one is a trap:

- The airflow enumeration is **Low, High, Middle, Auto**. It is not
  low-medium-high, and reading it as such mislabels the fan speed on every
  unit.
- The mode *status* object has a documented hole at value 4. It never reports
  Auto; it reports what the unit settled on. An unlabelled value is shown as
  its number rather than guessed at.
- `CommunicationStatus` is **active when communication is abnormal**. The
  polarity is the opposite of what the name suggests.
- The malfunction code object's value is 1–512 against a table Daikin
  publishes separately. The two-character code — `U1`, `E7` — is in the
  object's **Description** property, so a fault is two reads. The second one
  is only spent on units actually in alarm.
- Air direction is not a contiguous enumeration: 0–3 horizontal, 4 vertical,
  7 swing.
- Instance numbers are computable, not a table:
  `instance = ac_number × 256 + member`, with the AC number running in DIII
  address order across four ports of four lines of sixteen groups. The
  integration derives them, and the tests generate a full 256-group gateway
  from the same arithmetic.

## Diagnostics

**Settings → Devices & services → Daikin D-BACS → Download diagnostics**
reports what the gateway said about itself, which groups are live, absent and
dead, how many points were asked for and answered, which subscriptions were
accepted or refused, and any fault codes currently showing.

## Development

```bash
pip install pytest voluptuous bacpypes3
pytest tests -q
```

The suite stubs Home Assistant when it is not installed, so it runs without a
400 MB dependency tree — see `tests/_ha_stub.py` for what that covers and why.
The synthetic gateway in `tests/fake_gateway.py` is generated from the
instance formula, so the real BACnet client is the thing under test and no
test needs data from a real installation.

## Roadmap

See [PRD.md](PRD.md) for the full plan. In short: Phase 2 adds `climate`
entities and a head-end-aware write path; Phase 3 adds areas, card selection
and a self-building dashboard through
[HA-Cards](https://github.com/rellis-erigon/HA-Cards); Phase 4 adds synthetic
outdoor units, accumulated power on the energy dashboard, and heat reclaim
ventilators.

## Trademarks

See [TRADEMARKS.md](TRADEMARKS.md). This project is independent and
unaffiliated.
