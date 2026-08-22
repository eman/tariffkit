# MQTT publishing

Publishes the current price at each hour boundary, retained, with Home
Assistant MQTT Discovery so sensors create themselves. **No custom component
needed for this path**: if you already run an MQTT broker, this is the least
moving parts.

## Setup

```bash
pip install 'tariffkit[mqtt]'
```

Confirm your config first; the publisher inherits it:

```bash
tariffkit info
```

## Run

```bash
# publish once and exit, to check it works
tariffkit mqtt --broker 192.168.1.100 --once -v

# run continuously
tariffkit mqtt --broker 192.168.1.100

# authenticated broker: credentials are only sent over TLS
TARIFFKIT_MQTT_USERNAME=tariffkit TARIFFKIT_MQTT_PASSWORD=secret \
  tariffkit mqtt --broker mqtt.example --port 8883 --tls
```

| Flag | Default | |
|---|---|---|
| `--broker` | required | Broker host or IP |
| `--port` | 1883 | |
| `--username` | keyring/environment | Store `mqtt.password` with `tariffkit credentials set`; passwords are not accepted in argv |
| `--tls` | off | |
| `--allow-insecure-auth` | off | Permit credentials without TLS only on an isolated trusted network |
| `--topic-prefix` | `tariffkit` | |
| `--forecast-hours` | 48 | Hours in the forecast payload |
| `--no-discovery` | n/a | Skip the Home Assistant discovery config |
| `--once` | n/a | Publish once and exit (good for cron) |

It sleeps until the next hour boundary rather than polling, so it costs
essentially nothing to leave running.

Supplying a username or password without TLS is rejected, and a password always
requires a username. Anonymous plaintext connections remain supported. For an
authenticated broker, use TLS on port 8883:

```toml
[mqtt]
broker = "mqtt.example"
port = 8883
tls = true
```

If a broker on an isolated LAN cannot support TLS, explicitly acknowledge the
risk with `allow_insecure_auth = true`, `--allow-insecure-auth`, or
`TARIFFKIT_MQTT_ALLOW_INSECURE_AUTH=true`. Credentials can then be observed by
anyone able to inspect that network, so this escape hatch is not appropriate
across the internet or an untrusted LAN.

## Topics

| Topic | Payload |
|---|---|
| `tariffkit/import_price` | `0.37267` |
| `tariffkit/export_price` | `0.07212` |
| `tariffkit/spread` | `-0.30055` (export − import) |
| `tariffkit/tou_period` | `off_peak` |
| `tariffkit/daily_fixed_charge` | `0.79343` (USD/day, not per kWh) |
| `tariffkit/components/import/{generation,distribution,transmission,surcharges,credits,other}` | `0.15377` — one stackable band of the import price |
| `tariffkit/components/export/{generation,delivery,credits,other}` | `0.88896` — one stackable band of the export credit |
| `tariffkit/components/{direction}/{group}/attributes` | The tariff lines rolled into that band |
| `tariffkit/forecast` | Full JSON curve |
| `tariffkit/{import_price,export_price}/attributes` | Component breakdown and group roll-up, plus EMHASS and Predbat payloads |
| `tariffkit/spread/attributes` | Flat hourly forecast list |
| `tariffkit/status` | `online` / `offline` (last will) |

All published **retained**, so a subscriber connecting mid-hour gets the
current price immediately instead of waiting up to an hour.

Watch it:

```bash
mosquitto_sub -h 192.168.1.100 -t 'tariffkit/#' -v
```

## Home Assistant

With discovery enabled (the default), a **PG&E Rates** device appears with
Import Price, Export Price, Export Spread, TOU Period, Daily Fixed Charge, and
one sensor per component group in each direction. The `status` topic is wired
as the availability topic, so a crashed publisher shows the sensors as
unavailable rather than leaving stale prices looking live.

The component-group sensors are `sensor.tariffkit_import_generation`,
`_import_distribution`, `_import_transmission`, `_import_surcharges`,
`_import_credits`, `_import_other`, and on the export side `_export_generation`,
`_export_delivery`, `_export_credits`, `_export_other`. Each is in `USD/kWh`
and the groups of a direction sum to that direction's price, so stacking them
in a chart reproduces Import Price or Export Price exactly — see
[Component breakdown](home-assistant.md#component-breakdown) for what each
group contains and a ready-made stacked chart.

Daily Fixed Charge is `USD/day` rather than `USD/kWh` on purpose: it is the AB
205 Base Services Charge, billed per day of service, so nothing can stack it
against a marginal price by accident.

The 48-hour forecast rides along as an attribute on the spread sensor, shaped
as a flat hourly list:

```yaml
{{ state_attr('sensor.tariffkit_export_spread', 'forecast') }}
# [{"start": "...", "import": 0.37267, "export": 0.07212, "spread": -0.30055}, ...]
```

Prices are reported as plain measurements with a `USD/kWh` unit, **not**
`device_class: monetary`. Home Assistant rejects a monetary sensor whose unit
is not a bare currency code. That unit is still all the Energy dashboard's
price-entity validation asks for, so `sensor.tariffkit_import_price` and
`sensor.tariffkit_export_price` can be selected under grid consumption and
return to grid respectively.

### EMHASS and Predbat

The import and export attribute topics carry ready-made payloads for both:

```yaml
{{ state_attr('sensor.tariffkit_import_price', 'load_cost_forecast') }}
# [0.55214, 0.55214, 0.41273, ...]   dollars, 30-min slots, positional

{{ state_attr('sensor.tariffkit_import_price', 'raw_today') }}
# [{"from": "...", "to": "...", "rate": 55.214}, ...]   cents, 30-min slots
```

Setup is identical to the custom component, including the cents-for-pence
caveat — see [home-assistant.md](home-assistant.md#predbat) and
[EMHASS](home-assistant.md#emhass). Use the deterministic `sensor.tariffkit_*`
IDs here rather than the `sensor.pg_e_rates_*` ones.

Those attribute payloads are ~10 KB each and retained. That is fine for
Mosquitto, but brokers with a message size cap (AWS IoT is 128 KB) are worth
checking.

One genuine asymmetry with the custom component: MQTT Discovery has no way to
declare unrecorded attributes, so Home Assistant writes these payloads to the
recorder on every state change. Since the publisher only writes on the hour that
is 24 writes a day rather than 1,440, which is usually tolerable. Recorder cannot
exclude individual attributes — only whole entities — so the only lever is
dropping the entity's history entirely:

```yaml
recorder:
  exclude:
    entities:
      - sensor.tariffkit_import_price   # also loses the price history
```

If you want both interop payloads and price history, use the custom component,
which marks these attributes unrecorded and keeps the state.

## Run as a service

```ini
# /etc/systemd/system/tariffkit-mqtt.service
[Unit]
Description=tariffkit MQTT publisher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tariffkit
Environment=XDG_CONFIG_HOME=/etc/tariffkit
ExecStart=/opt/tariffkit/.venv/bin/tariffkit mqtt --broker 192.168.1.100 -v
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

With `XDG_CONFIG_HOME=/etc/tariffkit`, the config lives at
`/etc/tariffkit/tariffkit/config.toml`.

```bash
sudo systemctl enable --now tariffkit-mqtt
journalctl -u tariffkit-mqtt -f
```

### Or via cron

Since prices only change on the hour, `--once` from cron works too, and the
retained messages mean nothing is lost between runs:

```cron
0 * * * * /opt/tariffkit/.venv/bin/tariffkit mqtt --broker 192.168.1.100 --once
```

The long-running service is still preferable: it publishes an `offline` last
will if it dies, which cron cannot do.

## Troubleshooting

**Sensors do not appear.** Check discovery messages arrived:

```bash
mosquitto_sub -h 192.168.1.100 -t 'homeassistant/sensor/tariffkit/#' -v
```

Home Assistant's MQTT integration must be configured and its discovery prefix
must match `--discovery-prefix` (default `homeassistant`).

**Sensors show "unavailable".** The publisher is not running, or its last will
fired. Check `systemctl status tariffkit-mqtt`.

**Prices look wrong.** Run `tariffkit info` as the *service* user; a config
file in your own home directory is not visible to a systemd unit running as
someone else. This is the most common cause of a service reporting bundled PG&E
rates when you are on a CCA.
