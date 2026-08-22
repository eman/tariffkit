# TariffKit with Predbat

[Predbat](https://springfall2008.github.io/batpred/) plans battery charging and
discharging against a forecast of what electricity will cost. TariffKit supplies
that forecast. This page walks the whole path: installing both, wiring them
together, and confirming Predbat is actually reading your rates.

TariffKit publishes prices in the exact shape Predbat already reads, so there is
no template sensor, no proxy script, and no unit conversion on your side. What
you end up with:

```
TariffKit  ──  sensor.tariffkit_<profile>_import_price
               sensor.tariffkit_<profile>_export_price
                        │  raw_today / raw_tomorrow attributes
                        ▼
Predbat    ──  metric_octopus_import / metric_octopus_export in apps.yaml
                        │
                        ▼
               a charge/discharge plan priced against your real tariff
```

For the TariffKit side in depth — every sensor, the Energy dashboard, account
history — see [Home Assistant](home-assistant.md). This page assumes you want
Predbat specifically.

## Before you start

| | |
|---|---|
| Home Assistant | Any recent release; the integration is UI-configured |
| Time zone | **Must be `America/Los_Angeles`** — see [Time zone](#time-zone) |
| Tariff | A PG&E residential plan TariffKit models (E-1, E-ELEC, E-TOU-C, E-TOU-D, EV2-A) |
| Hardware | An inverter and battery Predbat supports, already reporting to Home Assistant |

TariffKit provides **prices only**. Predbat also needs load, PV, state-of-charge,
and inverter-control entities, which come from your inverter integration, not
from TariffKit. If you have not got Predbat talking to your hardware yet, do that
first using its own [installation guide](https://springfall2008.github.io/batpred/install/)
— this page picks up at the point where Predbat runs but has no rate data.

## 1. Install TariffKit

Via HACS as a custom repository (it is
[awaiting default-store inclusion](https://github.com/hacs/default/pull/10019)):

1. **HACS → three-dot menu → Custom repositories**
2. Add `https://github.com/eman/tariffkit`, category **Integration**
3. Search HACS for **TariffKit**, open it, **Download**
4. Restart Home Assistant
5. **Settings → Devices & services → Add integration → TariffKit**

The config flow asks for your supplier, rate plan, interconnection year, and
Permission-To-Operate date. [Home Assistant → Configure](home-assistant.md#configure)
covers every field. Manual installation is documented there too.

## 2. Turn on Predbat compatibility

**This is the step people miss.** The `raw_today` / `raw_tomorrow` attributes are
off by default, and until you enable them Predbat has nothing to read.

**Settings → Devices & services → TariffKit → Configure → Forecast and Predbat**

| Setting | Set it to |
|---|---|
| Enable Predbat compatibility | **on** |

That is the only setting Predbat needs. **The forecast horizon on the same page
does not affect it** — the Predbat payload is built from the engine rather than
from the shared forecast, and is always anchored to local midnight so that today
and tomorrow are complete by construction. Set the horizon for whatever else you
use the forecast for; it cannot truncate `raw_tomorrow`.

Leaving the toggle off costs nothing: the integration never computes the payload
at all rather than computing and hiding it.

## 3. Confirm the attributes are there

Before touching Predbat's configuration, check that Home Assistant is publishing
what Predbat needs. **Developer tools → States**, filter for `import_price`.

Your entity IDs derive from the profile and device names you chose during setup,
so confirm the real ones here rather than assuming — this page writes
`sensor.tariffkit_home_import_price` for a profile named `home`, but yours may
differ.

The attributes should look like this:

```yaml
raw_today:
  - from: "2026-08-22T00:00:00-07:00"
    to: "2026-08-22T00:30:00-07:00"
    rate: 33.358
  # ... 48 entries
raw_tomorrow:
  - from: "2026-08-23T00:00:00-07:00"
  # ... 48 entries
```

What to check:

- **Both lists are present and non-empty.** If they are missing entirely, step 2
  did not take effect.
- **48 entries each** — 30-minute slots aligned to `:00` and `:30`, matching
  Predbat's default `plan_interval_minutes: 30`. Both days are always complete,
  because the lists are anchored to local midnight rather than to the current
  hour, so Predbat never has to backfill a partial day. On the two DST
  changeover days the correct count is **50 in autumn and 46 in spring**, since
  the local day genuinely has 25 or 23 hours — that is right output, not a
  fault.
- **`rate` is a cents-scale number** (33.358, not 0.33358). See
  [Currency](#currency).
- **No `predbat_warning` attribute.** If one is present, read it — see
  [Time zone](#time-zone).

## 4. Install Predbat

Predbat installs either as a Home Assistant add-on or as an AppDaemon app; its
[installation guide](https://springfall2008.github.io/batpred/install/) covers
both and is the authority. Follow it through to the point where Predbat starts
and writes `predbat.*` entities, then come back here for the rate wiring.

The one detail worth knowing for the next step is where `apps.yaml` lives, which
depends entirely on how you installed Predbat — the add-on, AppDaemon and
standalone layouts all differ, and its installation guide names the path for
each. Predbat picks its config root from the first of `/config`, `/conf`,
`/homeassistant` and its working directory that **exists**, so on a host where
an unrelated `/config` is present it can settle somewhere you did not intend.
If it cannot find your file, its log prints the root it chose (`Config root is
...`) — start there rather than guessing.

## 5. Point Predbat at TariffKit

Two lines in `apps.yaml`:

```yaml
pred_bat:
  # Predbat keeps its own time zone, and it defaults to Europe/London.
  # Setting Home Assistant to Pacific does not change it. See Time zone below.
  timezone: America/Los_Angeles

  metric_octopus_import: 'sensor.tariffkit_home_import_price'
  metric_octopus_export: 'sensor.tariffkit_home_export_price'
```

Substitute the entity IDs you confirmed in step 3.

> **You do not need the Octopus Energy integration.** `metric_octopus_import` is
> just Predbat's name for "an entity carrying rate attributes" — Predbat is a UK
> tool and Octopus was the first supplier it supported. It probes the entity you
> name for `all_rates`, `rates`, `raw_today`, and `prices`, in that order, and
> reads whichever it finds. No Octopus service is involved.

Predbat reads the **attributes** and ignores the entity's state, so the state
being in dollars while the attributes are in cents is expected and harmless.

### Standing charge

Optional, but worth doing. Predbat ships a stock regex for
`metric_standing_charge` that only matches Octopus entities; on your system it
matches nothing, disables itself, and Predbat plans with a standing charge of
zero. TariffKit publishes the real figure:

```yaml
  metric_standing_charge: 'sensor.tariffkit_home_daily_fixed_charge'
```

Confirm this entity's real ID the same way as in step 3 — depending on how the
device was named at setup it may carry a device prefix, as in
`sensor.living_room_tariffkit_home_daily_fixed_charge`.

The units line up: TariffKit publishes `USD/day` (`0.79343`) and Predbat
multiplies by 100 to reach cents per day.

### What TariffKit does not configure

Everything else in `apps.yaml` — `load_today`, `pv_today`, `soc_max`,
`charge_rate`, the inverter definitions — describes your hardware and has
nothing to do with tariffs. Predbat's docs cover those.

## 6. Verify

Restart Predbat and read its log (**add-on → Log**, or `predbat.log` in its
config directory). A working ingest looks like:

```
Import rates: min 33.36c, max 55.21c, average 38.86c
- Import rates are cheap (33.4c - 39.0c) for the next 2 hours and then
  very expensive (55.2c) for the next 5 hours.
Info: Completed run status Demand
```

Cross-check the `min` against the `rate` you saw in step 3 — they should be the
same number. If they differ by exactly 100x, see [Currency](#currency).

You should **not** see:

```
Warn: Octopus: No Octopus data in sensor <entity> attribute 'all_rates' / 'rates' / 'raw_today' / 'prices'
Error: metric_octopus_import is not set correctly in apps.yaml, or no energy rates can be read
Error: Import rates are all zero, not able to compute a plan
```

These three arrive together and all mean the same thing: Predbat found the
entity but not the attributes. Go back to step 2.

Predbat's own plan is at **its web interface → Plan**, and shows each slot's
price alongside the charge/discharge decision it drove.

## Quieting the logs

Predbat publishes large attribute blobs on entities like `predbat.plan_html` and
`predbat.load_energy`. Home Assistant's recorder refuses to store attributes over
16 KB and logs a warning every time, which can run to thousands of lines a day:

```
WARNING (Recorder) [homeassistant.components.recorder.db_schema] State attributes
for predbat.plan_html exceed maximum size of 16384 bytes. This can cause database
performance issues; Attributes will not be stored
```

It is benign — state values still record, only the oversized attribute
dictionaries are dropped — but it buries everything else. Excluding Predbat's own
domain silences it:

```yaml
# configuration.yaml
recorder:
  exclude:
    domains:
      - predbat
```

This leaves `sensor.predbat_*` history untouched; only the `predbat.*` domain,
which holds the oversized internal entities, is dropped. Restart Home Assistant
to apply — recorder settings are read at startup.

## Currency

TariffKit publishes rates in **cents**, and Predbat will label them `p`.

Predbat assumes pence per kWh, and a number of its thresholds and defaults are
tuned to that magnitude. Publishing dollars would leave every one of them off by
100x. Predbat also inspects the key names to decide whether to rescale: the
`from` / `to` / `rate` shape TariffKit uses tells it the values are already in
minor units, so it applies no further conversion.

The optimisation is correct. Only the currency symbol lies.

## Time zone

**Two settings must both say `America/Los_Angeles`**, and they are independent:

| Where | |
|---|---|
| Home Assistant | **Settings → System → General → Time zone** |
| Predbat | `timezone:` in `apps.yaml` — **defaults to `Europe/London`** |

Predbat reads its own `timezone` key and falls back to `Europe/London` when it
is absent, so an instance can have Home Assistant correctly on Pacific while
Predbat still indexes the tariff against a London day. Setting one does not set
the other, and only the Home Assistant half produces a `predbat_warning`, so a
missing `timezone:` in `apps.yaml` fails silently.

`raw_today` and `raw_tomorrow` are anchored to the Pacific calendar day, because
that is what PG&E's tariff day means. Predbat derives its slot indices from
local midnight. Left at another zone, the first several hours of `raw_today`
land in Predbat's yesterday and the whole plan shifts by the offset.

With Predbat mode enabled on a non-Pacific instance, the price sensors carry a
`predbat_warning` attribute saying exactly this.

Worth eyeballing the plan on the two DST changeover days, since Predbat indexes
slots by minutes from midnight. The autumn transition has 50 half-hour slots and
the spring one 46, and on the autumn day two pairs share a wall clock (01:00 and
01:30 occur at both `-07:00` and `-08:00`). The entries carry explicit offsets and
are distinct instants, but a consumer keying purely on wall-clock time will see
one of each pair mask the other.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `No Octopus data in sensor ... attribute 'all_rates' / 'rates' / 'raw_today' / 'prices'` | Predbat compatibility is off (step 2), or `apps.yaml` names the wrong entity |
| `Import rates are all zero, not able to compute a plan` | Same cause — this is the downstream symptom of the line above |
| Rates off by exactly 100x | Something between TariffKit and Predbat is rescaling. Read the attributes directly in Developer tools; TariffKit's `rate` should already be cents |
| Plan shifted by a fixed number of hours | Home Assistant is not on `America/Los_Angeles`. See [Time zone](#time-zone) |
| Plan shifted by exactly 8 hours | `timezone:` missing from `apps.yaml`, so Predbat defaults to `Europe/London`. Home Assistant's own setting does not cover this |
| `raw_today` has 50 or 46 entries | Correct on the DST changeover days. See [3](#3-confirm-the-attributes-are-there) |
| Standing charge shows as zero | `metric_standing_charge` still holds Predbat's stock Octopus regex, which matches nothing. See [Standing charge](#standing-charge) |
| Thousands of `exceed maximum size of 16384 bytes` warnings | Predbat's attribute blobs hitting the recorder. See [Quieting the logs](#quieting-the-logs) |
| Warnings about `octopus_intelligent_slot`, `octopus_ready_time`, `octopus_charge_limit`, `octopus_saving_session` | Expected. Those are Octopus Intelligent features; with no Octopus integration they correctly disable themselves |

> **A note on translating the rate shape yourself.** Do not. TariffKit already
> emits Predbat's native `from` / `to` / `rate`, so a template sensor or script
> that copies the rates into another entity adds a second representation that has
> to be kept in sync by hand — and it will break silently the next time either
> side's attribute names change. Point `apps.yaml` at TariffKit's sensor
> directly.

## See also

- [Predbat on Sigenergy](predbat-sigenergy.md) — Sigenergy SigenStor specifics:
  which entities to map, the sign and unit conversions it needs, and the
  control question
- [Home Assistant](home-assistant.md) — the full integration: every sensor, the
  Energy dashboard, account history, diagnostics
- [Home Assistant → Predbat](home-assistant.md#predbat) — the same wiring from the
  TariffKit side, with the payload's guarantees spelled out
- [MQTT](mqtt.md) — the broker path, which publishes `raw_today` / `raw_tomorrow`
  unconditionally rather than behind an opt-in
- [Library](library.md) — `predbat_payload()` for building the same structure in
  your own Python
