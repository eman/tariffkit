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
| TOU Period | `peak` / `part_peak` / `off_peak` |
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
