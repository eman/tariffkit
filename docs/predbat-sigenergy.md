# TariffKit with Predbat on Sigenergy

A Sigenergy-specific companion to [Predbat](predbat.md). That page gets rates
flowing from TariffKit into Predbat and applies to any inverter; this one covers
what a Sigenergy SigenStor adds on top — the entity mapping, two sign and unit
conversions that silently corrupt a plan if you skip them, and the control
question, which is where most Sigenergy setups actually stall.

Read [Predbat](predbat.md) first and get to the point where Predbat logs
`Import rates: min ... max ... average ...`. Everything here assumes that works.

```
TariffKit ─────────────┐
  import/export price  │
                       ▼
Sigenergy ──────►  Predbat  ──────►  battery commands
  sensor.sigen_*    inverter_type: "SIG"      (see Control, below)
  via Modbus
```

## Before you start

| | |
|---|---|
| Sigenergy integration | Installed in Home Assistant, publishing `sensor.sigen_*` entities |
| Predbat | Running, with rates already arriving from TariffKit |
| Modbus access | Read at minimum; **write access is a separate question** — see [Control](#control) |

This page uses the entity names produced by the Sigenergy local Modbus
integration — `sensor.sigen_plant_*` for plant-level figures and
`sensor.sigen_inverter_*` for per-inverter ones. Confirm your own in
**Developer tools → States**; naming varies between integration versions.

## 1. Choose plant-level entities

A SigenStor exposes both plant-level and per-inverter versions of most figures.
**Use the plant-level ones.** With a single inverter they agree, but the plant
totals already include third-party PV and any DC EV charger, which is what
Predbat needs to reason about whole-site import and export.

The distinction is not cosmetic on a system with third-party solar:
`sensor.sigen_plant_daily_pv_energy` counts only Sigenergy's own PV and can read
`0.0` all day, while `sensor.sigen_plant_pv_daily_generation` is the real total.

## 2. Map the entities in apps.yaml

```yaml
pred_bat:
  inverter_type: "SIG"
  num_inverters: 1

  # Daily energy totals
  load_today:
    - sensor.sigen_plant_daily_load_consumption
  import_today:
    - sensor.sigen_plant_daily_grid_import_energy
  export_today:
    - sensor.sigen_plant_daily_grid_export_energy
  pv_today:
    - sensor.sigen_plant_pv_daily_generation

  # Instantaneous power
  load_power:
    - sensor.sigen_plant_total_load_power
  pv_power:
    - sensor.sigen_plant_pv_power
  grid_power:
    - sensor.sigen_plant_grid_active_power
  battery_power:
    - sensor.sigen_plant_battery_power

  # Sign conventions -- see below, these are not defaults
  grid_power_invert: true
  battery_power_invert: false

  # Battery state
  soc_percent:
    - sensor.sigen_plant_battery_state_of_charge
  soc_max:
    - sensor.sigen_plant_rated_energy_capacity
  battery_temperature:
    - sensor.sigen_battery_temperature_c   # a template sensor, see Units
```

## Sign conventions

Predbat documents its own convention in its Sigenergy component
(`sigenergy.py`):

```
batPower:    positive = discharging, negative = charging
activePower: positive = export,      negative = import
```

**Sigenergy's grid sensor is the reverse.** `sensor.sigen_plant_grid_active_power`
reads negative while exporting — you can confirm it against
`sensor.sigen_plant_grid_export_power`, which is unsigned and positive at the
same moment. So `grid_power_invert: true` is required. Without it Predbat
believes you are importing while you export, and every dispatch decision
inverts. This is the single most consequential setting on this page.

Battery power is the one to verify rather than trust. The integration exposes
two binary sensors that make it unambiguous:

```
binary_sensor.sigen_plant_battery_charging
binary_sensor.sigen_plant_battery_discharging
```

Watch `sensor.sigen_plant_battery_power` while one of those is `on`. If the
value is positive while *discharging*, keep `battery_power_invert: false`. If it
is positive while *charging*, set it to `true`. Do not infer this from a reading
of `0.0`.

## Units

Predbat converts units itself, but only by SI prefix — `k`, `M`, `m`
(`predbat.py`, `unit_conversion`). Sigenergy publishes power in kW where Predbat
wants W, and that conversion is handled automatically: the entity carries
`unit_of_measurement: kW`, Predbat asks for `W`, and the prefix rule applies.
Nothing to do.

> **Temperature is not converted, and this one bites.** The prefix rule finds no
> `k`/`M`/`m` in `°F` or `°C`, so no branch fires and the value passes through
> **unchanged**. On a US system reporting `86.9 °F`, Predbat reads `86.9 °C`. It
> logs `Warn: unit_conversion - Units mismatch ... expected °C, got °F after
> conversion` and then uses the number anyway. Battery temperature feeds the
> charge-rate derating curve, so a plan built on a phantom 87 °C battery is
> wrong in a way that is easy to miss.

If your Sigenergy integration reports °F, convert it with a template sensor and
point `battery_temperature` at that:

```yaml
# configuration.yaml
template:
  - sensor:
      - name: Sigen Battery Temperature C
        unique_id: sigen_battery_temperature_c
        unit_of_measurement: "°C"
        device_class: temperature
        state_class: measurement
        state: >
          {{ ((states('sensor.sigen_plant_ess_average_cell_temperature') | float(68) - 32) * 5 / 9) | round(2) }}
```

Check the source entity's unit first — an integration set to metric publishes °C
already, and the template is then unnecessary.

## State of charge in kWh

`soc_kw` is worth care on a SigenStor. The obvious candidate,
`sensor.sigen_plant_available_max_discharging_capacity`, can exceed
`sensor.sigen_plant_rated_energy_capacity` because it includes the DC EV
charger's contribution rather than the ESS alone.

Predbat accepts the inconsistency without complaint, which is precisely why it
is worth checking rather than assuming. If the two disagree, derive the figure
instead:

```yaml
  soc_kw:
    - sensor.sigen_battery_energy_kwh   # template: soc_percent * rated_capacity / 100
```

Predbat also accepts `soc_percent` plus `soc_max` alone; supplying a wrong
`soc_kw` is worse than supplying none.

## Control

This is where a Sigenergy setup usually stops, and it is worth understanding
before you plan around it.

The local Modbus integration is **read-rich and write-poor**. A typical install
publishes ~130 sensors but a control surface of only:

```
number.sigen_inverter_active_power_percentage_adjustment
number.sigen_inverter_power_factor_adjustment
number.sigen_inverter_dc_charger_max_charging_power_limit      (EV charger)
number.sigen_inverter_dc_charger_max_discharging_power_limit   (EV charger)
```

There is no battery charge/discharge power control, no target SOC, no reserve,
and no writable operating-mode select — `sensor.sigen_plant_ems_work_mode` is a
sensor, not a `select`. Predbat's `SIG` profile expects power-based charge
control (`output_charge_control: "power"`), and none of the above provides it
for the ESS.

Three ways forward:

| Option | What it gives you |
|---|---|
| **Monitoring only** | Predbat plans and shows what it *would* do. Genuinely useful for validating a tariff before committing to automation. Set **Read Only mode** in Predbat's own settings |
| **Remote EMS via Modbus** | If your integration and plant permit RW Modbus, enabling remote EMS exposes writable power setpoints. Availability depends on integration version and Sigenergy's own permissions |
| **Predbat's Sigenergy Cloud API component** | Predbat's supported control path: OAuth2 against Sigenergy's OpenAPI plus its MQTT broker for charge/discharge commands. Needs an AppKey/AppSecret from Sigenergy, and is independent of the Modbus integration |

Start with monitoring. The rate plumbing from [Predbat](predbat.md) and the
mapping above are identical in all three cases, so nothing is wasted — decide
about control once you can see whether the plans are worth executing.

## Verify

With rates already working, look for the inverter line in Predbat's log:

```
Inverter 0 with soc_max 16.12kWh, nominal_capacity 16.12kWh, ... temperature 30°C
```

Check each figure against Home Assistant:

- **`soc_max`** matches `sensor.sigen_plant_rated_energy_capacity`. If it reads a
  round number like 24.0 you are still on a default, not your hardware.
- **`temperature`** is plausible in °C. An 80–90 reading means the °F conversion
  above is missing.
- **Grid direction agrees with reality.** Compare Predbat's view against
  `binary_sensor.sigen_plant_exporting_to_grid` — if that is `on`, Predbat must
  not think you are importing.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Plan charges at peak and exports at trough | `grid_power_invert` missing. See [Sign conventions](#sign-conventions) |
| `Warn: unit_conversion - Units mismatch ... expected °C, got °F` | Temperature not converted. See [Units](#units) |
| Charge rate mysteriously derated | Same cause — Predbat thinks the battery is at ~87 °C |
| `soc_max` shows a round default (8, 24) | `soc_max` unmapped or unreadable; Predbat fell back |
| `pv_today` reads 0 all day despite production | Mapped to `daily_pv_energy` (Sigenergy PV only) instead of `pv_daily_generation`. See [1](#1-choose-plant-level-entities) |
| Battery charges when it should discharge | `battery_power_invert` backwards. Verify with the binary sensors |
| Plans look right but nothing happens | Expected without a control path. See [Control](#control) |

## See also

- [Predbat](predbat.md) — the rate wiring this page builds on
- [Home Assistant](home-assistant.md) — the TariffKit integration in full
