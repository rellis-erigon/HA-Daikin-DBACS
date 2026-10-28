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

### Knowing a change took

A write the gateway accepts is a request, not a result: the unit can refuse
it, clamp it, or miss it. So every change from the thermostat is followed
until the unit's own **status** object, read fresh from the gateway after
the write went out, shows it — mode on the mode status, not the mode
command. The setpoint, which is one read/write object and is shown the
moment it is written, is confirmed only by a reading taken after the write,
never by its own echo. A unit in Auto counts as confirmed when it reports
the Cool or Heat it chose.

The climate entity carries where things stand:

- `pending_changes` — each change not yet confirmed, as `setting`
  (`hvac_mode`, `power`, `temperature`, `fan_mode`, `swing_mode`), `value`
  and `state`: `held` while the write-rate guard spaces it, `sent` once it
  has gone.
- `last_change` — how the newest one ended: `result` is `confirmed`,
  `not_confirmed` (nothing showed it within two sweeps and 30 seconds; the
  unit's `reported` value is given and a warning is logged) or `refused`
  (with the `reason`).

For checking a mapping by hand, the climate entity also carries where the
unit lives: `gateway`, `gateway_device_instance`, `gateway_host`,
`ac_number`, `diii_address`, the mapping's `tag`, `system` and `outdoor`,
and `bacnet_objects` — every object the group publishes, with its point,
object name, type, instance and whether it is a command or a status. These
are fixed, so they are kept out of the recorder.

The HA-Cards controller card reads these to say *sending*, *confirmed by
the unit* or *not confirmed: the unit still reports …* on the unit itself.

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
but off by default, because per-unit metering is a separately licensed
option. Where it is not fitted, the gateway still publishes both objects and
holds them at zero forever, which looks like a broken meter rather than an
absent one. Enabling them then buys two long-term-statistics series per
indoor unit that record nothing.

A zero does not by itself prove the option is missing, since a fitted meter
also reads zero until something is consumed. Read the rest of the
Accumulator before deciding: an unfitted one still answers with a plausible
name, the right engineering units, `no-fault-detected`, `out-of-service` 0
and empty status flags, so only a unit that has demonstrably been running
with a flat total tells you the meters are not wired. Gas total is only
meaningful behind gas-engine outdoor plant.

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

### A unit that was here and has gone quiet is an alarm

Unavailable is right for a reading and wrong for an alarm: it is grey on a
dashboard and fires nothing that keys on `on`. So every group the gateway
has ever published live is remembered in the config entry, and that unit,
dead or alive, carries a *Not responding* problem sensor that stays
available and turns **on** when it stops answering, with the reason:
`DIII-NET communication fault`, `not answering reads`, `no longer published
by the gateway`, or `gateway not answering`. Because the list survives a
restart, and a unit with a device in Home Assistant counts as found, a unit
that is already dead when Home Assistant starts still raises it.

- **Each outdoor unit** gets *Not responding* too: on when any of its indoor
  units is, with `all_not_responding` true when every one of them is, which
  points at the outdoor unit or its wiring rather than at the rooms.
- **The gateway** gets *Units in alarm*: on when any unit is not responding
  or not Normal (Alarm reads Malfunction), and any outdoor unit with no
  indoor unit answering or in malfunction, each listed in its attributes.
  This is the one to hang a notification on.

**Fault codes say what they mean.** A unit's *Malfunction code* sensor, an
outdoor unit's *Fault code* and the *Units in alarm* lists carry Daikin's
standard meaning beside the code (`E5 (inverter compressor locked or
overloaded)`), from the VRV service manual's table in `codes.py`. It is a
starting point for the engineer, not a diagnosis: the same code can have more
than one cause, and a code not in the table is shown alone, never guessed.

A unit removed on purpose: `daikin_dbacs.recheck` with `forget_missing: true`
stops expecting the units the gateway no longer publishes at all.

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

## Choosing the card, in bulk

A generated card is drawn with a *faceplate*, and there are three ways to
say which — in order of precedence, so a deliberate choice is never
overridden by a general one:

**1. Per unit, in the mapping file.** For the unit that really does have a
BRC1E63 on the wall beside it:

```json
{ "1 \u00b7 1-01": { "name": "Room A2 ACU A-02", "faceplate": "daikin-brc1e63" } }
```

**2. In bulk, from the dashboard.** One line restyles every unit the view
draws, and it is the answer to "I want a better card than the default on
all of them":

```yaml
strategy:
  type: custom:bridge-devices
  bridges: [daikin_dbacs]
  faceplate: fcu-unit
```

**3. Nothing** — and every unit is drawn with `indoor-unit`, the full
status face.

The faceplates for this card are `indoor-unit` (everything the unit
reports), `fcu-unit` (a compact tile, for grids of hundreds), and
`daikin-brc1e63`, `daikin-brc2e61`, `daikin-brc1h63k` and `daikin-brc315d7`
for the wall controllers they emulate.

### Why this does not change Home Assistant's own Overview

Home Assistant's auto-generated dashboard is produced by its `original-states`
strategy, which draws its own tile and entity cards and has no mechanism for
being told to use a custom one. There is no setting, anywhere, that makes it
prefer a different card for a device.

So a dashboard that is both automatic *and* uses these cards has to be built
by a strategy that generates them, which is what `custom:bridge-devices` is
for. Keep Home Assistant's own view and add one beside it:

```yaml
views:
  - title: Home
    strategy: {type: original-states}
  - title: Air Conditioning
    strategy: {type: custom:bridge-devices, bridges: [daikin_dbacs]}
```

Both views stay automatic — new units appear in either without anybody
editing a dashboard.

## Site data stays out of this repository

The gateway knows an indoor unit by its DIII-NET address and nothing else —
there is no name object and every description is empty. So room names,
areas and equipment tags come from a mapping file you maintain, read at
runtime:

```yaml
action: daikin_dbacs.apply_mapping
data:
  dry_run: true          # reports; set false to apply
```

Each gateway reads its own file, `/share/daikin_dbacs/gateway-<device instance>-units.json`
(for a gateway whose BACnet device instance is 11, `gateway-11-units.json`). The
folder is outside the configuration directory on purpose: it is site data, and
nothing in this repository ships or needs one. Without a file the integration
still works — units keep their address names, and the model, rated kW and
refrigerant systems are simply absent. The file is keyed by the DIII address
as the integration reports it:

```json
{
  "1 \u00b7 1-00": {
    "name": "Room A1 Bed ACU A-01",
    "area": "Block A",
    "tag": "ACU A-01",
    "system": "S-01",
    "outdoor": "CU-S-01",
    "outdoor_area": "Plant Deck",
    "model": "FXDQ40TV1B"
  }
}
```

**`system` and `outdoor` make the outdoor units.** Each refrigerant system
becomes a device (named by `outdoor`) under the gateway, and every indoor
unit on it hangs off that device, so an outdoor unit's page lists the indoor
units it powers. Its `outdoor_area` (any one unit on the system can carry
it) places it, because a condenser serving several floors has no area to
inherit; an outdoor unit you have placed yourself is left where it is.

> **Upgrading from 0.4:** the folder was `/share/HA-Daikin-DBACS-site`. Move the
> `gateway-*-units.json` files to `/share/daikin_dbacs/` and restart.

**A `model` makes the unit's rating usable.** The device shows the model
instead of the gateway's generic one, and the climate entity carries
`model`, `rated_cooling_kw` and `rated_heating_kw`: Daikin's nominal duty
for the model's capacity class (FXDQ40 is the 40 class, 4.5 kW cooling and
5.0 kW heating), read from the digits after the series letters. It is a
rating for sizing and heat-load sums, not a measurement. It is read at
setup, so a changed model needs the entry reloaded and a new integration
version a restart.

The address is read forgivingly — `"1 · 1-00"`, `"1 1-00"`, `"1-1-00"` or an
explicit `ac_number` all work, because the file is maintained by hand.

Two things it will not do. **A device you renamed or placed yourself is left
alone**, because you knew something the file does not; pass `overwrite: true`
if you mean it. And **it reports before it acts** — `dry_run` defaults to
true, since this renames every device it matches.

**No mapping is shipped here, and none ever will be.** This repository is
public and `scripts/check-no-site-detail.py` fails the build on real room
numbers, equipment tags, outdoor unit names and private IP addresses. Run it
before committing anything you copied off a site:

```bash
python3 scripts/check-no-site-detail.py
```

A real room number reached a test file here within a day of the mapping work
starting. That is why it is a check and not a note.

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

See [PRD.md](PRD.md) for the full plan. Phases 1–3 are in: read-only
discovery, two-way control with the write-rate guard and change
confirmation, and naming, areas and cards through
[HA Cards](https://github.com/rellis-erigon/HA-rellis-erigon-Cards). Of Phase 4,
the synthetic outdoor units are in, and the accumulator and ventilation points
are available but off by default; the PRD lists what is left.

## Trademarks

See [TRADEMARKS.md](TRADEMARKS.md). This project is independent and
unaffiliated.
