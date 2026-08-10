# Home Assistant

Two ways in. Pick one.

| | MQTT | Custom component |
|---|---|---|
| Needs a broker | yes | no |
| Runs where | anywhere | inside HA |
| Setup | one CLI command | copy files, restart, UI config flow |
| Config | your config file | HA's UI |

If you already run an MQTT broker, **use the MQTT path**; see
[mqtt.md](mqtt.md). It is fewer moving parts and does not need HA restarts. The
custom component is the better fit if you would rather keep everything inside
Home Assistant and configure it in the UI.

## Custom component install

### Manual

```bash
cd /config    # your Home Assistant config directory
mkdir -p custom_components
cp -r /path/to/nem-rates/custom_components/nem_rates custom_components/
```

Restart Home Assistant, then **Settings → Devices & Services → Add Integration
→ "PG&E NEM 3.0 Rates"**.

### HACS

Not in the default HACS store. Add this repository as a custom repository
(category: Integration), install, restart.

### The dependency

`manifest.json` declares `nem-rates>=0.1.0`, which Home Assistant installs from
PyPI on first setup. **This package is not published to PyPI yet**, so that step
will fail. Until it is published, install the library into HA's Python
environment yourself:

```bash
# Home Assistant Container / Supervised
docker exec -it homeassistant pip install /share/nem_rates-0.1.0-py3-none-any.whl

# Core install in a venv
/srv/homeassistant/bin/pip install /path/to/nem_rates-0.1.0-py3-none-any.whl
```

Build the wheel with `uv build` from the repo root; it lands in `dist/`.

## Configuration

The config flow asks for supplier, interconnection year, PTO date, ACC Plus
segment, CARE/FERA discount, Base Services Charge tier, forecast hours, and,
for CCA service, CCA name, PCIA vintage, franchise fee surcharge, and export
generation rate. Values are validated against the library before the entry is
created, so a bad combination is rejected in the form rather than at runtime.

Change anything later via **Configure** on the integration; it reloads in place.

> The UI config flow does not yet expose the vendored CCA **rate cards** (the
> `rate_card` / `option` keys). MCE customers configuring through the UI must
> enter generation rates manually, or use the MQTT path, which reads the config
> file and does support rate cards.

## Entities

| Sensor | Unit |
|---|---|
| Import Price | USD/kWh |
| Export Price | USD/kWh |
| Export Spread | USD/kWh (export − import) |
| TOU Period | enum: `peak` / `part_peak` / `off_peak` |
| Base Services Charge | USD/day (disabled by default) |

All under one **PG&E Rates** device. The Base Services Charge is disabled by
default because it is a fixed daily amount, not a marginal price, and mixing it
into energy dashboards produces nonsense.

**Entity IDs are assigned by Home Assistant**, derived from the device name and
the sensor name, typically `sensor.pg_e_rates_import_price`. Confirm the actual
IDs under **Settings → Devices & Services → PG&E Rates → entities** before
writing automations against them, and rename there if you want something
shorter. The examples below use `sensor.pg_e_rates_*`; substitute whatever your
install assigned.

The MQTT path is different: it pins `object_id` in the discovery payload, so
those entities are deterministically `sensor.nem_rates_import_price`,
`sensor.nem_rates_export_price`, `sensor.nem_rates_spread`, and
`sensor.nem_rates_tou_period`.

Import and export sensors carry the full component breakdown as attributes,
plus the forecast:

```yaml
{{ state_attr('sensor.pg_e_rates_export_price', 'components') }}
{{ state_attr('sensor.pg_e_rates_export_spread', 'forecast') }}
```

They also carry the payloads that other energy systems read directly; see
[Energy dashboard](#energy-dashboard), [EMHASS](#emhass), and
[Predbat](#predbat) below.

> The forecast, EMHASS, and Predbat attributes are excluded from the recorder
> (`_unrecorded_attributes`), so they will not appear in history or the logbook.
> That is deliberate: the coordinator recomputes every minute, and writing a
> 48-hour curve to the database 1,440 times a day would bloat it for no gain.
> The current state of every sensor is recorded as normal.

## Energy dashboard

The price sensors work as-is. Home Assistant's price-entity validation requires
only a numeric state and a unit ending in `/kWh`, `/MWh`, or `/Wh` — it checks
neither `device_class` nor `state_class`, and does not compare the currency
against your instance's. `USD/kWh` qualifies.

**Settings → Dashboards → Energy → Electricity grid:**

| Field | Entity |
|---|---|
| Grid consumption → "Use an entity with current price" | `sensor.pg_e_rates_import_price` |
| Return to grid → "Use an entity with current price" | `sensor.pg_e_rates_export_price` |

Both directions accept a price entity, so export compensation tracks the real
NBT credit hour by hour rather than a flat assumed rate.

The Base Services Charge stays out of this on purpose — it is a fixed daily
amount, not a marginal price, and the Energy dashboard multiplies price by kWh.
Add it as a separate fixed cost if you want it in a bill total.

## EMHASS

The import and export sensors expose EMHASS's two cost parameters directly, as
bare lists of dollars per kWh at 30-minute resolution — matching the
`optimization_time_step: 30` that EMHASS ships with.

```yaml
rest_command:
  emhass_mpc:
    url: "http://localhost:5000/action/naive-mpc-optim"
    method: POST
    content_type: "application/json"
    payload: >
      {
        "prediction_horizon": {{ state_attr('sensor.pg_e_rates_import_price', 'prediction_horizon') }},
        "load_cost_forecast": {{ state_attr('sensor.pg_e_rates_import_price', 'load_cost_forecast') | tojson }},
        "prod_price_forecast": {{ state_attr('sensor.pg_e_rates_export_price', 'prod_price_forecast') | tojson }},
        "pv_power_forecast": {{ state_attr('sensor.your_solar_forecast', 'watts') | tojson }}
      }
```

The two series are split across the two sensors rather than bundled into one
`runtimeparams` blob because you need to merge your own PV and load forecasts
into the same call.

**These lists are positional, not timestamped.** EMHASS matches value *n* to its
own slot *n*, so the first value has to be the slot EMHASS is currently in. The
integration handles this by dropping already-elapsed slots on every refresh —
at 10:45 the list starts at 10:30, not 10:00. `prediction_horizon` shrinks to
match, which is why it is published alongside and why the call should use it
rather than a hardcoded number.

If you have changed `optimization_time_step` from its default, the resolution
here will not match and EMHASS will misread the horizon.

The span is your configured **forecast hours** (default 48), so 96 half-hour
values less whatever has elapsed in the current hour.

## Predbat

Point Predbat at the price sensors. It reads the `raw_today` / `raw_tomorrow`
attributes and ignores the state:

```yaml
# apps.yaml
metric_octopus_import: 'sensor.pg_e_rates_import_price'
metric_octopus_export: 'sensor.pg_e_rates_export_price'
```

Entries are 30-minute slots aligned to `:00` and `:30`, matching Predbat's
default `plan_interval_minutes: 30`. Both days are always complete: the lists are
anchored to local midnight, not to the current hour, so Predbat never has to
backfill a partial day by copying the previous one.

> **Rates are published in cents, and Predbat will label them `p`.** Predbat is a
> UK tool: it assumes pence per kWh, and several of its thresholds and defaults
> are tuned to that magnitude. Publishing dollars would leave every one of them
> off by 100x. The optimisation is correct — only the currency symbol lies.

**Your Home Assistant instance must be set to `America/Los_Angeles`.** These
lists are anchored to the Pacific calendar day, because that is what PG&E's
tariff day means. Predbat derives its slot indices from Home Assistant's local
midnight, so on an instance left at UTC the first several hours of `raw_today`
land in Predbat's yesterday and the whole plan shifts by the offset. Check
**Settings → System → General → Time zone**.

Worth eyeballing Predbat's plan on the two DST days, since it indexes slots by
minutes from midnight. The autumn transition has 50 half-hour slots and the
spring one 46, and on the autumn day two pairs share a wall clock (01:00 and
01:30 occur at both `-07:00` and `-08:00`). The entries carry explicit offsets
and are distinct instants, but a consumer that keys purely on wall-clock time
will see one of each pair mask the other.

## Automation examples

Discharge the battery when exporting beats self-consumption:

```yaml
automation:
  - alias: Export when the spread is positive
    trigger:
      - platform: numeric_state
        entity_id: sensor.pg_e_rates_export_spread
        above: 0
    condition:
      - condition: numeric_state
        entity_id: sensor.battery_state_of_charge
        above: 40
    action:
      - service: select.select_option
        target: { entity_id: select.battery_mode }
        data: { option: "Export" }
```

Charge during the cheapest upcoming hours:

```yaml
template:
  - sensor:
      - name: Cheapest import hour today
        state: >
          {% set f = state_attr('sensor.pg_e_rates_export_spread', 'forecast') %}
          {{ (f | sort(attribute='import') | first).start if f else 'unknown' }}
```

## Troubleshooting

**Integration will not load.** Almost always the missing `nem-rates` library;
see the dependency note above. Check **Settings → System → Logs**.

**Prices look like bundled PG&E when you are on a CCA.** Reconfigure the
integration and set supplier to CCA, then supply generation rates. Verify
against your bill using the method in
[configuration.md](configuration.md#verifying-against-a-bill).

**Export price looks far too low.** Expected on CCA service: PG&E pays you only
the delivery component. Check the `complete` attribute: `false` means CCA
generation compensation is not configured and the figure understates reality.

**Sensors stop updating.** The coordinator recomputes every minute from local
data; there is no network call to fail. A stall means the integration errored;
check the logs.
