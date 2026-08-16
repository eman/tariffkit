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
Home Assistant, configure it in the UI, and use the Energy dashboard's native
price-entity support directly.

The rest of this page covers the custom component.

For integration development, the repository includes a Docker Compose stack
that bind mounts both the component and Home Assistant configuration. See
[Containers](containers.md#test-the-home-assistant-integration).

## Install

### HACS

TariffKit is awaiting inclusion in the default HACS store. Until that review is
complete, add it as a custom repository with category **Integration**:

[![Open your Home Assistant instance and add the TariffKit repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=eman&repository=tariffkit&category=integration)

Select the newest release, install, and restart Home Assistant. HACS downloads
the release's `tariffkit.zip`, which contains only the integration files. The
default branch is deliberately not offered: it can temporarily refer to a
Python package version that has not been published yet.

### Manual

```bash
cd /config    # your Home Assistant config directory
mkdir -p custom_components
cp -r /path/to/tariffkit/custom_components/tariffkit custom_components/
```

Restart Home Assistant, then **Settings → Devices & Services → Add Integration
→ "TariffKit"**.

### The dependency

`manifest.json` declares an exact-pinned `tariffkit==0.2.2` requirement, which
Home Assistant installs from PyPI on first setup. The requirement stays an
exact version rather than a range so a HACS update to the integration and a
package upgrade cannot drift apart: every GitHub integration release has one
matching PyPI distribution.

The integration computes from local static data and does not sign in to PG&E,
MQTT, InfluxDB, or another Home Assistant instance, so it deliberately does not
request or store those credentials. Home Assistant config entries are access
controlled but not an encrypted secret vault; adding unused passwords there
would increase exposure without enabling a feature.

### Removal

**Settings → Devices & Services → TariffKit → ⋮ → Delete** removes the
config entry and every entity and device it created. Nothing about it lives
outside that entry: there is no separate cache, database table, or file on
disk to clean up afterward. If you installed manually, also delete
`custom_components/tariffkit` (or remove the HACS custom repository) so a
restart does not offer to set it up again.

## Configure

**Settings → Devices & Services → Add Integration → "TariffKit"** opens a
first choice:

- **Configure manually** — a short, staged wizard:
  1. **Account and tariff** — a profile name, generation supplier,
     rate plan, and a toggle for whether to configure export compensation at
     all (turn it off for a pure-import account with no solar or battery).
  2. **Delivery and export** — only the fields your first answers make
     relevant: interconnection year, Permission-To-Operate date (optional —
     leave it blank if you do not have one yet; it does not block finishing
     setup, but export prices remain marked `locked: false` until TariffKit can
     calculate the nine-year lock end), ACC Plus segment, and CARE/FERA
     discount appear only when export is enabled; baseline territory and code
     appear only for E-TOU-C; Base Services Charge income tier is always asked.
  3. **CCA product** — only when you chose a Community Choice Aggregator as
     your supplier: its vendored rate card and product option. Raw
     generation-rate overrides are not asked here — they are an advanced,
     per-account detail that reaches the integration only through
     **Import TariffKit profile** below, or later through account history's
     own edit form once a profile carries them.
- **Import TariffKit profile** — paste JSON produced by `tariffkit account
  export NAME` (or another Home Assistant instance's **Export profile**,
  under [Account history](#account-history)). This is the only path that
  carries a multi-epoch history, CCA raw generation-rate overrides, and
  meter-source mappings into the entry directly, and it is how you move a
  profile you already maintain on the CLI into HA.

Every field is validated against the library before the entry is created, so
an invalid combination is rejected in the form with the same error the CLI
would raise, not discovered later at runtime.

Forecast horizon and Predbat compatibility mode are **not** asked during
setup — they default to sensible values (48 hours, Predbat off) and live
under **Configure → Forecast and Predbat** afterward, as their own menu item
rather than mixed into pricing settings. Keeping them out of initial setup
means the two or three questions most people need to answer are the only
ones on screen.

Multiple TariffKit entries can coexist — one per account or service
agreement, or more than one meter in the same household — since each gets
its own identity from the config entry itself, not from the tariff and dates
you happened to enter. Profile names are the stable local identity, so Home
Assistant rejects a second entry with the same normalized profile name.

Change anything later via **Configure** on the integration; it reloads in
place, without a restart.

## Account history

**Configure** opens a menu with three choices:

| Menu item | Does |
|---|---|
| Account pricing settings | The same staged wizard as setup (account/tariff → delivery/export → CCA product), editing the epoch in force today. An import-only epoch reopens with export still off, rather than defaulting back on and re-asking for delivery fields you never entered. |
| Forecast and Predbat | Forecast horizon and the Predbat compatibility toggle. Stored as options; changing either never touches account history. |
| **Account history** | Opens the sub-menu below. |

Its entry holds a whole [named account profile](accounts.md) — an ordered,
dated history of settings — not just today's values, so account history is
its own sub-menu rather than flat top-level choices:

| Sub-menu item | Does |
|---|---|
| Inspect account history | Read-only: every epoch's effective date and tariff. |
| Add account transition | Add a new dated epoch — the full settings form, defaulting to today and the profile's current values. |
| Edit account transition | Pick an existing epoch by its effective date, then edit its settings; a CCA epoch imported with raw generation-rate overrides keeps them editable here even though the basic wizard never asks for them. |
| Remove account transition | Pick an epoch, then confirm with a checkbox; leaving it unchecked returns to the menu without changing anything. |
| Import profile | Paste JSON from `tariffkit account export`, replacing the stored profile outright. |
| Export profile | Read-only JSON for pasting into `tariffkit account update ... --config-json`, or into another Home Assistant instance's **Import profile**. |

Every save reloads the entry immediately, so an edited epoch or a freshly
imported profile takes effect without a restart.

**Editing settings needs an epoch already in force today.** A profile whose
earliest epoch is dated in the future -- for example, one imported ahead of
a move-in date -- has no configuration in force yet. Opening **Account pricing settings** or **Add account transition** on a
profile like that aborts cleanly with "This account profile has no pricing
configuration in force yet." rather than showing a broken or empty form.
Inspecting, editing, or removing an existing transition; importing or
exporting a profile; and **Forecast and Predbat** are unaffected, since none
of them need today's active settings. Wait for the epoch's effective date,
edit an existing future transition, or replace the profile through **Import
profile** with one that already starts today.

**The integration never stores PG&E credentials.** It strips a profile's
`credential_set` before saving it, since this integration never signs in to
the portal itself; a profile arriving through **Import profile** with a
credential set keeps its epochs and evidence but loses that reference. Set
one on the profile through the CLI instead if you use `account sync` there.

**Older config entries migrate automatically and safely.** An entry created
before this schema exists has no stored profile, just old flat settings (or
an earlier profile shape); loading it synthesizes or upgrades a profile,
falling back to a single epoch dated `1970-01-01` — a sentinel meaning "as
far back as it matters" — when no explicit effective date was ever recorded.
Migration preserves the entry's prices and every entity's existing unique ID
and history, so upgrading does not silently reset an Energy dashboard's cost
graph; a migration TariffKit cannot make sense of fails the entry with a
logged reason rather than guessing.

## Entities and devices

| Sensor | Unit | Notes |
|---|---|---|
| Import Price | USD/kWh | |
| Export Price | USD/kWh | |
| Import/Export Spread | USD/kWh | export − import; excludes battery efficiency, degradation, and inverter losses |
| TOU Period | enum: `peak` / `part_peak` / `off_peak` | |
| Rate Forecast Through | timestamp | state is how far the cached forecast currently reaches; see [History and forecast timeline](#history-and-forecast-timeline) |

There is deliberately no fixed-charge entity: the AB 205 Base Services Charge
is a $/day amount, not a $/kWh marginal price, and mixing it into an Energy
dashboard produces nonsense. Read it separately via
`engine.daily_fixed_charge()` if you need it (see [Library](library.md)).

Every sensor sits under one device, named from your account or profile name —
`TariffKit — home` for a profile named `home`, or plain `TariffKit Rates` if
you configured manually without one. Confirm the actual entity IDs under
**Settings → Devices & Services → TariffKit → entities** before writing
automations against them; the examples below assume a profile named `home`
and use `sensor.tariffkit_home_*` — substitute whatever your install assigned.
The device's model reflects the active tariff (and export vintage, once one
applies) and updates automatically when your history's active epoch changes;
its manufacturer is your utility, and it exposes a configuration URL only
when the tariff's own published source is a real web address.

The MQTT path is different: it pins `object_id` in the discovery payload, so
those entities are deterministically `sensor.tariffkit_import_price`,
`sensor.tariffkit_export_price`, `sensor.tariffkit_spread`, and
`sensor.tariffkit_tou_period`.

Import and export carry the component breakdown, quality flags, and
provenance as attributes:

```yaml
{{ state_attr('sensor.tariffkit_home_export_price', 'components') }}
{{ state_attr('sensor.tariffkit_home_export_price', 'quality') }}
```

Import/Export Spread carries `quality`, `provenance`, and a fixed `description`
disclosing what it excludes. TOU Period carries `season` and `quality`. None
of the four current-value sensors carry a forecast array any more — that
lives on **Rate Forecast Through**, and EMHASS's two series are actions, not
attributes; see [History and forecast timeline](#history-and-forecast-timeline),
[EMHASS](#emhass), and [Predbat](#predbat) below.

> Large, frequently-changing attributes (the forecast's `rates` list, and
> Predbat's `raw_today` / `raw_tomorrow` when enabled) are excluded from the
> recorder (`_unrecorded_attributes`). That is deliberate: the coordinator
> recomputes every minute, and writing a 48-hour curve to the database 1,440
> times a day would bloat it for no gain. The current numeric state of every
> sensor is still recorded normally, which is what native History graphs and
> the Energy dashboard read.

## Energy dashboard

The price sensors work as-is. Home Assistant's price-entity validation
requires only a numeric state and a unit ending in `/kWh`, `/MWh`, or `/Wh` —
it checks neither `device_class` nor `state_class`, and does not compare the
currency against your instance's. `USD/kWh` qualifies.

**Settings → Dashboards → Energy → Electricity grid:**

| Field | Entity |
|---|---|
| Grid consumption → "Use an entity with current price" | Import Price |
| Return to grid → "Use an entity with current price" | Export Price |

Both directions accept a price entity, so export compensation tracks the real
NBT credit hour by hour rather than a flat assumed rate. The Base Services
Charge stays out of this on purpose — add it as a separate fixed cost if you
want it in a bill total.

Because attributes are lean now, the Energy dashboard's own price history
comes entirely from the sensors' recorded numeric states — nothing about this
redesign changes what the Energy dashboard shows; it changes what else rides
along on the same entities.

## History and forecast timeline

Home Assistant has no generic API for a sensor to publish *future* values
into a graph — the recorder and every native History/Logbook view only ever
show what a sensor's state *was*; the built-in state graph cannot plot a
future timestamp no matter which card you use. TariffKit's forecast lives
entirely in the **Rate Forecast Through** sensor's unrecorded `rates`
attribute: a list of `{start, end, import, export, spread}` points reaching
from now out to the account's configured forecast horizon.

**Fallback with no custom card:** add Import Price, Export Price, and
Import/Export Spread to a native **History** graph card for the live,
recorded past, and add **Rate Forecast Through** to an **Entities** card so
the forecast horizon and its `rates` attribute are at least inspectable. This
works with nothing beyond what ships in Home Assistant Core, but does not
draw the forecast as a line — only the native History path is limited that
way; it is not a TariffKit restriction.

**One continuous timeline with the [apexcharts-card](https://github.com/RomRider/apexcharts-card)
HACS frontend card** (not shipped by TariffKit — install it separately, `v2.2.3`
or later): one card can mix the Recorder's real history with a client-side
`data_generator` series in the same chart, so the same three sensors' solid,
*recorded* past extends into the forecast sensor's dashed, *future* half at a
shared "now" marker:

```yaml
type: custom:apexcharts-card
graph_span: 24h
span:
  start: hour
  offset: "-12h"
now:
  show: true
  label: Now
header:
  show: true
  title: Import / export rates
series:
  - entity: sensor.tariffkit_home_import_price
    name: Import (recorded)
    color: "#b87333"
    extend_to: now
    group_by:
      func: last
      duration: 15min
  - entity: sensor.tariffkit_home_export_price
    name: Export (recorded)
    color: "#2a9d8f"
    extend_to: now
    group_by:
      func: last
      duration: 15min
  - entity: sensor.tariffkit_home_rate_forecast_through
    name: Import (forecast)
    color: "#b87333"
    stroke_dash: 6
    curve: stepline
    data_generator: |
      return entity.attributes.rates.map((r) => {
        return [new Date(r.start).getTime(), r.import];
      });
  - entity: sensor.tariffkit_home_rate_forecast_through
    name: Export (forecast)
    color: "#2a9d8f"
    stroke_dash: 6
    curve: stepline
    data_generator: |
      return entity.attributes.rates.map((r) => {
        return [new Date(r.start).getTime(), r.export];
      });
```

`extend_to: now` (not the deprecated `extend_to_end`) draws each recorded
series' last known value forward to the `now` marker instead of stopping
short of it, so the solid line meets the dashed one with no gap between
them; `stroke_dash` is what actually renders the forecast half dashed rather
than merely faded, so recorded fact and forecast read as visually distinct
at a glance even in a screenshot with no legend. Add a fifth series against
Import/Export Spread, styled the same way, if you want it on the same axis.
`curve: stepline` matches the flat, hourly-block nature of TOU pricing
rather than interpolating between hours. If `quality.complete` or
`quality.exact` is `false` for the active epoch or export vintage, style
that stretch distinctly (a `data_generator` can branch on `point.quality`
from the `points` shape returned by [Get rates](#get-rates) if you need a
richer per-point quality readout than the sensor's aggregate).

## Actions

Two [response-returning actions](https://www.home-assistant.io/docs/scripts/perform-actions/#use-templates-to-set-data-for-actions)
are registered once per Home Assistant instance (not per entry), so they stay
callable — including from **Developer Tools → Actions**, where the config
entry selector lists every loaded TariffKit entry — even before any entry has
finished loading.

### Get rates

`tariffkit.get_rates` returns a bounded, timestamped forecast at a chosen
slot resolution.

| Field | Required | Default | Notes |
|---|---|---|---|
| `config_entry` | yes | | Which TariffKit account to price with. |
| `start` | no | | Offset-aware timestamp, aligned to `resolution`. Plain text field, not Home Assistant's native date/time picker. |
| `end` | no | | Offset-aware timestamp; cannot combine with `horizon`. Plain text field, not Home Assistant's native date/time picker. |
| `date` | no | | Pacific calendar date to start at midnight; cannot combine with `start`/`end`. |
| `horizon` | no | 24 | Hours from `start` (or now, or `date`) when `end` is not given. 1–168. |
| `resolution` | no | 60 | Slot size in minutes: 15, 30, or 60. |

**`start` and `end` are text fields, deliberately not Home Assistant's
built-in date/time selector.** That selector has no way to express an
explicit UTC offset -- it resolves to your instance's local time zone -- and
this action requires the offset written out, so any of `-07:00`, `-08:00`,
or `Z` in the value itself rather than implied by your instance's settings.
Type the full string, for example `2026-08-16T00:00:00-07:00`, whether
calling from **Developer Tools → Actions**, a script, or an automation.

```yaml
action: tariffkit.get_rates
data:
  config_entry: <your TariffKit entry>
  date: "2026-08-16"
  horizon: 24
  resolution: 30
response_variable: forecast
```

returns

```json
{
  "start": "2026-08-16T00:00:00-07:00",
  "end": "2026-08-17T00:00:00-07:00",
  "resolution": 30,
  "points": [
    {"start": "2026-08-16T00:00:00-07:00", "end": "2026-08-16T00:30:00-07:00",
     "import": 0.41231, "export": 0.1882, "spread": -0.22411,
     "quality": {"complete": true, "exact": true, "locked": true}},
    "... one entry per slot ..."
  ],
  "quality": {"complete": true, "exact": true, "locked": true},
  "generated_at": "2026-08-16T00:00:03-07:00",
  "provenance": {
    "segments": [
      {"start": "2026-08-16T00:00:00-07:00", "end": "2026-08-17T00:00:00-07:00",
       "utility": "PGE", "tariff": "E-ELEC", "account_profile": "home",
       "export_vintage": "nbt26", "tariff_source": "https://..."}
    ]
  }
}
```

Provenance is effective-dated over the requested window. A window crossing an
account-profile epoch or tariff snapshot has one ordered entry per contiguous
segment, each with exact `start` and `end` boundaries; it is never labeled with
the coordinator's current configuration.

Timestamps without an explicit UTC offset, and timestamps that fall on the
autumn DST fold (ambiguous between `-07:00` and `-08:00`), are rejected with a
`ServiceValidationError` rather than silently guessed. A window that does not
align to `resolution`, that spans more than 168 hours, or that mixes
`start`/`end` with `date` is rejected the same way, with a message naming the
problem.

### Get EMHASS forecast

`tariffkit.get_emhass_forecast` takes the same window fields (`config_entry`,
`start`, `end`, `date`, `horizon`, `resolution` — defaulting to 30 minutes
here, matching EMHASS's shipped `optimization_time_step`), returning EMHASS's
own runtime-parameter shape instead of TariffKit's:

```yaml
action: tariffkit.get_emhass_forecast
data:
  config_entry: <your TariffKit entry>
  horizon: 24
response_variable: emhass
```

```json
{
  "load_cost_forecast": [0.41231, 0.41231, "..."],
  "prod_price_forecast": [0.1882, 0.1882, "..."],
  "prediction_horizon": 48,
  "start": "2026-08-16T09:00:00-07:00",
  "end": "2026-08-17T09:00:00-07:00",
  "resolution": 30,
  "quality": {"complete": true, "exact": true, "locked": true},
  "generated_at": "2026-08-16T09:00:03-07:00",
  "provenance": {
    "segments": [
      {"start": "2026-08-16T09:00:00-07:00", "end": "2026-08-17T09:00:00-07:00",
       "utility": "PGE", "tariff": "E-ELEC", "account_profile": "home",
       "export_vintage": "nbt26", "tariff_source": "https://..."}
    ]
  }
}
```

Both actions require the selected entry to be loaded, and both are capped at
a 168-hour window regardless of the entry's own configured forecast horizon
— they price on demand, independent of the coordinator's forecast setting.

## EMHASS

Call [`tariffkit.get_emhass_forecast`](#get-emhass-forecast) from a
`rest_command` (or a script that stores the response and forwards it):

```yaml
script:
  emhass_mpc:
    sequence:
      - action: tariffkit.get_emhass_forecast
        data:
          config_entry: <your TariffKit entry>
          horizon: 24
        response_variable: rates
      - action: rest_command.emhass_mpc
        data:
          prediction_horizon: "{{ rates.prediction_horizon }}"
          load_cost_forecast: "{{ rates.load_cost_forecast }}"
          prod_price_forecast: "{{ rates.prod_price_forecast }}"
          pv_power_forecast: "{{ state_attr('sensor.your_solar_forecast', 'watts') }}"
```

**These two lists are positional, not timestamped** — EMHASS's `method='list'`
path reads them straight into a DataFrame column and only checks the length,
matching value *n* to its own slot *n*. Call the action with no `start`/`date`
(or `date: today`) so the first value lines up with EMHASS's own current
slot; `prediction_horizon` is returned alongside so the call can use it
rather than a hardcoded number. If you have changed EMHASS's
`optimization_time_step` from its 30-minute default, pass a matching
`resolution` or the two will disagree about slot length.

## Predbat

Disabled by default. Enable it in **Configure → Forecast and Predbat**
("Enable Predbat compatibility") — until then, the import and export sensors
carry no `raw_today` / `raw_tomorrow` attributes and the integration never
computes that payload, so leaving it off costs nothing.

Once enabled, point Predbat at the price sensors; it reads the `raw_today` /
`raw_tomorrow` attributes and ignores the state:

```yaml
# apps.yaml
metric_octopus_import: 'sensor.tariffkit_home_import_price'
metric_octopus_export: 'sensor.tariffkit_home_export_price'
```

Entries are 30-minute slots aligned to `:00` and `:30`, matching Predbat's
default `plan_interval_minutes: 30`. Both days are always complete: the lists
are anchored to local midnight, not to the current hour, so Predbat never has
to backfill a partial day by copying the previous one.

> **Rates are published in cents, and Predbat will label them `p`.** Predbat is
> a UK tool: it assumes pence per kWh, and several of its thresholds and
> defaults are tuned to that magnitude. Publishing dollars would leave every
> one of them off by 100x. The optimisation is correct — only the currency
> symbol lies.

**Your Home Assistant instance must be set to `America/Los_Angeles`.** These
lists are anchored to the Pacific calendar day, because that is what PG&E's
tariff day means. Predbat derives its slot indices from Home Assistant's
local midnight, so on an instance left at another time zone the first several
hours of `raw_today` land in Predbat's yesterday and the whole plan shifts by
the offset. With Predbat mode enabled on a non-Pacific instance, the price
sensors carry a `predbat_warning` attribute saying exactly this — check
**Settings → System → General → Time zone** if you see it.

Worth eyeballing Predbat's plan on the two DST days, since it indexes slots by
minutes from midnight. The autumn transition has 50 half-hour slots and the
spring one 46, and on the autumn day two pairs share a wall clock (01:00 and
01:30 occur at both `-07:00` and `-08:00`). The entries carry explicit offsets
and are distinct instants, but a consumer that keys purely on wall-clock time
will see one of each pair mask the other.

## Diagnostics

**Settings → Devices & Services → TariffKit → ⋮ → Download diagnostics**
returns a sanitized JSON snapshot for troubleshooting: schema version, whether
the entry is loaded, forecast hours and Predbat mode, the active price
point's start/tariff/supplier, aggregate quality flags, the Predbat time zone
warning (if any), a trimmed provenance block (utility, tariff, supplier,
tariff effective date, export vintage), the cached forecast's span, Home
Assistant's configured time zone, and today's Pacific calendar date.

It deliberately omits the account's full profile, its observations, and any
meter-source mapping — nothing that could identify the account or reveal how
its bill has been reconciled, only what a report needs to say "here is which
rate rules were active and how trustworthy they are."

## Automation examples

Discharge the battery when exporting beats self-consumption:

```yaml
automation:
  - alias: Export when the spread is positive
    trigger:
      - platform: numeric_state
        entity_id: sensor.tariffkit_home_import_export_spread
        above: 0
    condition:
      - condition: numeric_state
        entity_id: sensor.battery_state_of_charge
        above: 40
    action:
      - action: select.select_option
        target: { entity_id: select.battery_mode }
        data: { option: "Export" }
```

Find the cheapest upcoming import hour from the forecast entity's `rates`
attribute:

```yaml
template:
  - sensor:
      - name: Cheapest import hour today
        state: >
          {% set points = state_attr('sensor.tariffkit_home_rate_forecast_through', 'rates') %}
          {{ (points | sort(attribute='import') | first).start if points else 'unknown' }}
```

For anything that needs a specific window rather than "whatever the
coordinator happens to have cached," call
[`tariffkit.get_rates`](#get-rates) from a script and act on its
`response_variable` instead of a template against sensor attributes.

## Quality checklist

TariffKit is not part of Home Assistant Core and does not claim an official
[Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
tier -- that requires acceptance into Core. **See
[home-assistant-quality.md](home-assistant-quality.md) for the full
rule-by-rule self-assessment**, including the one honest gap worth knowing
before you rely on this: nothing currently stops you from setting up the same
real account twice, since removing the old, mutable, tariff-derived unique ID
removed duplicate-account prevention along with it and nothing has replaced
it yet.

## Troubleshooting

**Integration will not load.** Almost always the missing `tariffkit` library;
see [The dependency](#the-dependency) above. Check **Settings → System →
Logs**.

**Prices look like bundled PG&E when you are on a CCA.** Reconfigure the
integration and set supplier to CCA, then supply its rate card (or generation
rates via **Import profile**). Verify against your bill using the method in
[configuration.md](configuration.md#verifying-against-a-bill).

**Export price looks far too low.** Expected on CCA service: PG&E pays you
only the delivery component. Check the `quality` attribute's `complete` flag:
`false` means CCA generation compensation is not configured and the figure
understates reality.

**Sensors stop updating.** The coordinator recomputes every minute from local
data; there is no network call to fail. A stall means the integration errored;
check the logs.

**An action call fails with a validation error.** The message names the
problem directly — an ambiguous DST timestamp, a window not aligned to the
requested resolution, a horizon over 168 hours, or an entry that is not
currently loaded. Fix the call rather than retrying; these are rejected by
design, not transient.

**Predbat's plan looks shifted by hours.** Check the Predbat time zone
warning on the price sensors, and your instance's time zone; see
[Predbat](#predbat) above.
