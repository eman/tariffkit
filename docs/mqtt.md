# MQTT publishing

Publishes the current price at each hour boundary, retained, with Home
Assistant MQTT Discovery so sensors create themselves. **No custom component
needed for this path** — if you already run an MQTT broker, this is the least
moving parts.

## Setup

```bash
pip install 'nem-rates[mqtt]'
```

Confirm your config first — the publisher inherits it:

```bash
nem-rates info
```

## Run

```bash
# publish once and exit, to check it works
nem-rates mqtt --broker 192.168.1.100 --once -v

# run continuously
nem-rates mqtt --broker 192.168.1.100
```

| Flag | Default | |
|---|---|---|
| `--broker` | required | Broker host or IP |
| `--port` | 1883 | |
| `--username` / `--password` | none | |
| `--tls` | off | |
| `--topic-prefix` | `nem_rates` | |
| `--forecast-hours` | 48 | Hours in the forecast payload |
| `--no-discovery` | — | Skip the Home Assistant discovery config |
| `--once` | — | Publish once and exit (good for cron) |

It sleeps until the next hour boundary rather than polling, so it costs
essentially nothing to leave running.

## Topics

| Topic | Payload |
|---|---|
| `nem_rates/import_price` | `0.37267` |
| `nem_rates/export_price` | `0.07212` |
| `nem_rates/spread` | `-0.30055` (export − import) |
| `nem_rates/tou_period` | `off_peak` |
| `nem_rates/forecast` | Full JSON curve |
| `nem_rates/{import_price,export_price}/attributes` | Component breakdown |
| `nem_rates/spread/attributes` | Flat hourly forecast list |
| `nem_rates/status` | `online` / `offline` (last will) |

All published **retained**, so a subscriber connecting mid-hour gets the
current price immediately instead of waiting up to an hour.

Watch it:

```bash
mosquitto_sub -h 192.168.1.100 -t 'nem_rates/#' -v
```

## Home Assistant

With discovery enabled (the default), a **PG&E Rates** device appears with
Import Price, Export Price, Export Spread, and TOU Period. The `status` topic
is wired as the availability topic, so a crashed publisher shows the sensors as
unavailable rather than leaving stale prices looking live.

The 48-hour forecast rides along as an attribute on the spread sensor, shaped
as a flat hourly list for planners like EMHASS:

```yaml
{{ state_attr('sensor.nem_rates_export_spread', 'forecast') }}
# [{"start": "...", "import": 0.37267, "export": 0.07212, "spread": -0.30055}, ...]
```

Prices are reported as plain measurements with a `USD/kWh` unit, **not**
`device_class: monetary` — Home Assistant rejects a monetary sensor whose unit
is not a bare currency code.

## Run as a service

```ini
# /etc/systemd/system/nem-rates-mqtt.service
[Unit]
Description=nem-rates MQTT publisher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nem-rates
Environment=XDG_CONFIG_HOME=/etc/nem-rates
ExecStart=/opt/nem-rates/.venv/bin/nem-rates mqtt --broker 192.168.1.100 -v
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

With `XDG_CONFIG_HOME=/etc/nem-rates`, the config lives at
`/etc/nem-rates/nem-rates/config.toml`.

```bash
sudo systemctl enable --now nem-rates-mqtt
journalctl -u nem-rates-mqtt -f
```

### Or via cron

Since prices only change on the hour, `--once` from cron works too, and the
retained messages mean nothing is lost between runs:

```cron
0 * * * * /opt/nem-rates/.venv/bin/nem-rates mqtt --broker 192.168.1.100 --once
```

The long-running service is still preferable: it publishes an `offline` last
will if it dies, which cron cannot do.

## Troubleshooting

**Sensors do not appear.** Check discovery messages arrived:

```bash
mosquitto_sub -h 192.168.1.100 -t 'homeassistant/sensor/nem_rates/#' -v
```

Home Assistant's MQTT integration must be configured and its discovery prefix
must match `--discovery-prefix` (default `homeassistant`).

**Sensors show "unavailable".** The publisher is not running, or its last will
fired. Check `systemctl status nem-rates-mqtt`.

**Prices look wrong.** Run `nem-rates info` as the *service* user — a config
file in your own home directory is not visible to a systemd unit running as
someone else. This is the most common cause of a service reporting bundled PG&E
rates when you are on a CCA.
