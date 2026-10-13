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
   **change-of-value** as they move. Everything else is read on a sweep,
   every two minutes by default.
5. Choose which points you want in **Configure** — once, applied to every
   group.

## Control is off until you ask for it

By default this integration only reads. Turn on **Allow Home Assistant to
control the air conditioning** in the integration's options and each group
gains a thermostat, a filter-reset button and switches for its other
commands.

It is off by default because these gateways usually sit under a building
management head-end, and because installing an update should never quietly
make a building writable. Three things stand between a service call and the
air conditioning:

- **A priority survey.** A BACnet write holds a priority slot until it is
  released, so before the first write to any point the integration reads that
  point's `Priority_Array`. If another client is holding it — usually the
  BMS — the write is refused and says so. A point held from Home Assistant
  looks entirely normal to the BMS while the building's own schedule stops
  working, which is the failure this exists to prevent. Home Assistant writes
  at **priority 16**, the lowest there is, so anything the building's own
  control holds sits above it.
- **A write-rate guard.** Daikin caps setting changes at **7,000 a year per
  point** — nineteen a day. An automation with a bad trigger would spend that
  in an afternoon, and the result is a worn-out gateway rather than an error
  message. Writes to one point are spaced at least 60 seconds apart, a
  rolling 24-hour count warns past half the allowance, and the daily cap is
  enforced by default.
- **Coalescing**, so the spacing is not felt. A change that arrives too soon
  replaces the one already waiting and only the last value is sent — nudging
  a setpoint three times sends two writes, not three errors.

The points that take control away from whoever is in the room — forced stop,
forced thermostat off, and the three that disable the wall controller — are
left out entirely unless you ask for them separately.

## What you get per indoor unit

**Every status point the unit reports, enabled by default** — eighteen of
them where the gateway publishes all eighteen:

| Entity | From |
|---|---|
| Running | `StartStopStatus` |
| Alarm | `Alarm` |
| Malfunction code | `MalfunctionCode`, as the two-character Daikin code |
| Mode | `AirConModeStatus` |
| Fan speed | `AirFlowRateStatus` |
| Room temperature | `RoomTemp` |
| Setpoint | `TempAdjust` |
| Filter | `FilterSign` |
| Thermostat | `ThermoStatus` |
| Compressor | `CompressorStatus` |
| Indoor fan | `IndoorFanStatus` |
| Heater | `HeaterStatus` |
| Air direction | `AirDirectionStatus` |
| Energy saving | `EnergyEfficiencyStatus` |
| Forced thermostat off | `ForcedThermoOFFStatus` |
| Communication | `CommunicationStatus` |
| Ventilation mode and amount | `VentilationMode/AmountStatus` (heat reclaim ventilators) |
| Fault | an `event` entity, so history shows *when* a unit faulted |

The two accumulators — accumulated power and accumulated gas — are available
but off by default, because they are model-dependent and read zero on units
that do not support them, which looks like a broken meter rather than an
absent one.

With control on, each group also gets a `climate` entity carrying mode,
action, both temperatures, fan speed and swing, a filter-reset `button`, and
`switch` entities for energy saving and forced thermostat off.

### Why a fault event as well as an alarm

An alarm binary sensor tells you a unit is faulted now. It does not tell you
that it faulted at 04:10, cleared at 04:40 and did it again an hour later,
which is the shape of an intermittent fault and the thing worth catching. The
event carries the Daikin code with it, and a unit that was already faulted
when Home Assistant started is not announced as a new fault.

Every entity carries the BACnet object it reads, its instance number and its
DIII-NET address as attributes, so a value can always be traced back to the
object it came from.

## Choosing points, in bulk

A fully expanded gateway could publish **256 groups of 33 readable points** —
nearly nine thousand. Real ones publish far fewer, and read faster than that
ceiling suggests: on a system with most of one DIII-NET port populated,
*every* member of *every* group reads in about four and a half seconds. Even
so, the point list is the load control, and it works on two axes:

- **By point, across every group.** "Give me setpoint on everything." "I
  never want heater status." Chosen once in **Configure** and applied to every
  group, present and future.
- **By group.** Narrow it to switch a floor or a wing off wholesale.

A point you have not chosen still exists in the entity registry, greyed out on
its device page, so Home Assistant's own entity list can bulk-enable it with
checkboxes. **What matters is that a disabled point is never read and never
subscribed** — the sweep is built from the chosen list only.

Changing that list takes effect immediately, including on points that were
disabled by a previous run. `entity_registry_enabled_default` only applies
when an entity is first created, so the integration reconciles the registry
against the chosen list at startup — otherwise adding a point would appear to
do nothing. A point you switched off by hand stays off; only ones the
integration disabled are brought back.

## Only the points your gateway actually has

Groups differ in what they publish, and so do firmware versions. On a real
system most groups carry neither Forced stop nor Reject sub-group control
while a couple carry both, and the ventilation members for heat reclaim
ventilators do not exist at all on firmware older than specification edition
G.

So the entity list is built from the gateway's own `Object_List` rather than
from a table of what the model *could* have. **An entity that could only ever
be unavailable is not created.** Diagnostics reports which groups publish
which set of members, because "why has this unit got fewer entities than that
one" is usually this.

If the gateway will not give up its object list, the integration falls back to
paging it, and then to probing every slot — which still finds every group, it
just cannot tell which members each one carries, so the full set is assumed
rather than invented.

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
- **The device instance is found for you, two ways.** A unicast Who-Is
  first; and when that goes unanswered — which is normal, because `I-Am` is
  broadcast and cannot cross a routed boundary — a plain ReadProperty on
  `device,4194303`, BACnet's unconfigured instance, which gateways answer
  with their real one. Entering it by hand is the third resort, not the
  second. It is set with Daikin's Trial Operation tool.

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
- **Member 18 is published as an `Accumulator`** even though the
  specification names no object for it and marks it Japanese-market-only. It
  is modelled, off by default, and labelled with the inference stated — the
  Accumulator type carries accumulated power and accumulated gas, and member
  19 is the power one.
- Binary present-values arrive as BACnet `BinaryPV` enumerations rather than
  as `0`/`1` or `"active"`, and temperatures arrive as float32 artefacts
  (`21.600000381469727`). Both are normalised.
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
