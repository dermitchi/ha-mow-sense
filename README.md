# Mow Sense

Gardena SILENO / Husqvarna Automower robot mowers in Home Assistant **over Bluetooth** (works through an ESPHome Bluetooth proxy) – no cloud, no account, and **Home Assistant decides when the mower drives**.

Built on the reverse-engineered [automower-ble](https://github.com/alistair23/AutoMower-BLE) protocol library. Tested in simulation; developed for a SILENO minimo 250.

## Why another integration?

The core *Husqvarna Automower BLE* integration keeps a permanent Bluetooth connection: it occupies a proxy slot, blocks the Gardena app, and one dropped link makes everything unavailable – and setup fails if the mower is off. Its *dock* only parks *until the next schedule start*, so the mower drives off again on whatever schedule the app (or the mower's auto schedule) created.

Mow Sense instead:

- **Short sessions** – connect, read, disconnect. Every 60 s while the mower is out, every 5 min in the station.
- **Keeps the last values** through Bluetooth dropouts (30 min by default); separate *Connected*, *In Bluetooth range* and *Last contact* entities show the link.
- **Sets up even when the mower is off** (winter) and polls immediately when it shows up again.
- **Automatic switch** – *off* = the mower only drives when HA starts it. If it is found on its own schedule anyway (app, button), it is sent back to *park until further notice*. Runs HA started are left alone.
- **Schedule from HA** (optional) – pick a `schedule` helper; its time blocks are written into the mower and re-checked every 10 min. Whatever the Gardena app puts there is overwritten. Because the schedule lives in the mower, it mows on time even when Bluetooth is down.
- **Clock sync** – keeps the mower clock right (it uses local time).
- **Blade reminder** – press *Blades changed* after fitting new blades; *Blades since change* counts cutting hours and *Blade change due* turns on after the interval (default 100 h, adjustable – Gardena gives no fixed figure).
- **Never nudges an idle mower** – switching *Automatic* off only changes the mode; a start/trigger is only sent when the mower is out in the garden.

## Entities

- **Lawn mower** – start (for the *Mowing duration on start*, default 3 h), pause, dock
- **Sensors** – battery, state, activity, mode, error (German text), next start, remaining charging time, total cutting/running time, blade usage, collisions, charging cycles, schedule in mower (+ sync status), last contact
- **Binary sensors** – charging, problem, connected, in Bluetooth range
- **Switch** – Automatic (own schedule)
- **Number** – mowing duration on start, blade change interval
- **Buttons** – park until further notice, refresh now, blades changed

Services: `mow_sense.mow` (hours), `mow_sense.park` (`further_notice` / `next_schedule`), `mow_sense.sync_schedule`.
Event `mow_sense_corrected` fires whenever Mow Sense had to correct the mower (mode or schedule).

## Setup

1. ESPHome Bluetooth proxy near the mower's station (`bluetooth_proxy: active: true`).
2. HACS → ⋮ → *Custom repositories* → `https://github.com/dermitchi/ha-mow-sense`, category *Integration*. Install **Mow Sense**, restart Home Assistant.
3. Switch the mower **off and on again** (pairing mode for a few minutes), then *Settings → Devices & services*: the mower is usually discovered; otherwise *Add integration → Mow Sense*. Enter the PIN if your mower has one.
4. Remove / disable the core *Husqvarna Automower BLE* integration for the same mower – two integrations fighting over one Bluetooth link causes exactly the dropouts this one avoids.
5. Optional: *Configure* → schedule helper, poll intervals, how long to keep values.

## Development

```
python3.13 -m venv .venv && .venv/bin/pip install pytest-homeassistant-custom-component automower-ble==0.2.9
.venv/bin/pytest
```

`tools/probe.py` reads a mower straight from a computer's Bluetooth (read-only, nothing moves).
