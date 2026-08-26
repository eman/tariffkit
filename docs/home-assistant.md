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

`manifest.json` declares an exact-pinned `tariffkit==0.4.0` requirement, which
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

## Upgrading

Nothing to do, in almost every case: entities keep their identity across
releases, the config entry is migrated in place, and the integration stores no
cache or file that could go stale. Three situations are worth knowing about
anyway, because two of them are about *ordering* rather than about repair.

### From a release, without metered energy configured

There is nothing to migrate. [Metered energy](#metered-energy) is opt-in and
creates no entities until you name a meter, so an instance that never used it
has none of the entities the notes below discuss. Update, restart, carry on.

### Turning on metered energy for the first time

Upgrade **first**, then configure the meters. The order matters: the running
totals write long-term statistics from the moment they exist, and an entity that
records history under one release and then changes meaning under the next leaves
a seam in that history which nothing detects on its own. Naming your meters on
the newest release you intend to run avoids the question entirely.

Then run [Backfilling history](#backfilling-history) once, leaving `start`
unset. That prices from the billing cycle containing your Permission To Operate
date, which is the only start where the export credit bank genuinely opens at
zero — see [And it should start at the cycle containing your PTO
date](#and-it-should-start-at-the-cycle-containing-your-pto-date).

### From a development checkout that had `net_cost` entities

Only relevant if you ran the integration from `main` between the release that
added metered energy and the one that renamed it. No published release ever
carried `net_cost` entities.

`sensor.*_net_cost_today` and `sensor.*_net_cost_this_cycle` are now
`amount_due_today` and `amount_due_this_cycle`, and the backfill's
`tariffkit:<profile>_net_cost` statistic is `tariffkit:<profile>_amount_due`.
The rename is deliberate rather than cosmetic. The figure changed meaning and
sign — it was charges less every credit earned, which goes negative; it is now
what a statement charges, which does not. Home Assistant accumulates a `total`
sensor's lifetime sum as `sum += new - old` whenever `last_reset` is unchanged,
and the cycle entity's `last_reset` is the cycle start, so keeping the old
entity id would have added the whole banked balance to that sum in a single
compile and never washed it out. A new id abandons the old series intact
instead of corrupting it.

So, after upgrading:

1. **Update anything that names the old entities** — dashboard cards, template
   sensors, automations. The old entities are removed from the registry on
   reload, so a reference to one resolves to nothing rather than to a wrong
   number.
2. **Re-run the backfill** over the same window. History published by the older
   code was priced as `Bill.total` and without the annual settlements, so it
   disagrees with what the entities now show.
3. **Delete the orphaned statistics** if you do not want them, under **Developer
   tools → Statistics**: the old `sensor.*_net_cost_*` series and
   `tariffkit:<profile>_net_cost`. They are inert, not wrong — nothing reads
   them — so this is housekeeping rather than repair.

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
     appear only for E-1 and E-TOU-C; Base Services Charge income tier and
     Medical Baseline enrollment are always asked.
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

Forecast horizon, Predbat compatibility mode, and metered energy are **not**
asked during setup — they default to sensible values (48 hours, Predbat off,
no meters) and live under **Configure → Forecast and Predbat** and
**Configure → Metered energy** afterward, as their own menu items rather than
mixed into pricing settings. Keeping them out of initial setup means the two
or three questions most people need to answer are the only ones on screen.

Metered energy in particular is deliberately not a setup question: pricing an
account does not require a meter, and the counters are usually integrated
after the tariff rather than before, so asking during setup would put a
question in front of every new user that most of them cannot answer yet.

Once you *have* named the meters, run
[Backfilling history](#backfilling-history) straight away rather than waiting
for the running totals to accumulate. The recorder's statistics for those meter
entities usually reach back months before the integration existed, and that
history is priceable from the moment the meters are configured.

Multiple TariffKit entries can coexist — one per account or service
agreement, or more than one meter in the same household — since each gets
its own identity from the config entry itself, not from the tariff and dates
you happened to enter. Profile names are the stable local identity, so Home
Assistant rejects a second entry with the same normalized profile name.

Change anything later via **Configure** on the integration; it reloads in
place, without a restart.

## Account history

**Configure** opens a menu with four choices:

| Menu item | Does |
|---|---|
| Account pricing settings | The same staged wizard as setup (account/tariff → delivery/export → CCA product), editing the epoch in force today. An import-only epoch reopens with export still off, rather than defaulting back on and re-asking for delivery fields you never entered. |
| Forecast and Predbat | Forecast horizon and the Predbat compatibility toggle. Stored as options; changing either never touches account history. |
| Metered energy | The grid import and export counters and the billing cycle start day. See [Metered energy](#metered-energy). Stored as options; clearing both entities removes the running-total entities on reload. |
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
| Export Minus Import | USD/kWh | export − import; excludes battery efficiency, degradation, and inverter losses |
| TOU Period | enum displayed as Peak / Part-peak / Off-peak | |
| Rates Available Through | timestamp | state is how far the cached forecast currently reaches; see [History and forecast timeline](#history-and-forecast-timeline) |
| Rate Data Status | diagnostic enum | PTO date, export lock end, NBT vintage, tariff provenance, source, and quality flags |
| Import Generation / Distribution / Transmission / Surcharges / Credits / Other | USD/kWh | the import price, split into stackable bands; see [Component breakdown](#component-breakdown) |
| Export Generation / Delivery / Credits / Other | USD/kWh | the export credit, split the same way |
| Daily Fixed Charge | USD/day | AB 205 Base Services Charge; **not** a per-kWh price and not part of the stack |
| Grid import / export today | kWh | metered import and export since **Pacific** midnight — the tariff's billing day, not the instance's local one; only with [Metered energy](#metered-energy) configured |
| Energy cost / Export credit / Amount due today | USD | today's running charge, credit, and what a statement would charge for it, reported in USD regardless of the instance's configured currency; only with [Metered energy](#metered-energy) configured |
| Grid import / export this cycle | kWh | the same two counters over the billing cycle to date |
| Energy cost / Export credit / Amount due this cycle | USD | the same three figures over the billing cycle to date |
| Export credit bank (utility) / (generation) | USD | Net Billing credit carried between cycles, one per settling party; see [The export credit bank](#the-export-credit-bank). Only with a grid-export meter configured |

Daily Fixed Charge is reported in `USD/day`, not `USD/kWh`, because that is
what it is: a fixed daily amount, not a marginal price. The unit keeps it out
of the Energy dashboard's price pickers and out of any chart stacked against
`USD/kWh` series, which is the outcome you want — adding it to a marginal
price would misprice every kWh. Use it in a bill total, or read the same
number from `engine.daily_fixed_charge()` (see [Library](library.md)).

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

Import and export carry the per-line component breakdown, quality flags, and
provenance as attributes:

```yaml
{{ state_attr('sensor.tariffkit_home_export_price', 'components') }}
{{ state_attr('sensor.tariffkit_home_export_price', 'quality') }}
```

Export Minus Import carries `quality`, `provenance`, and a fixed `description`
disclosing what it excludes. TOU Period carries `season` and `quality`. None
of the four current-value sensors carry a forecast array any more — that
lives on **Rates Available Through**, and EMHASS's two series are actions, not
attributes; see [History and forecast timeline](#history-and-forecast-timeline),
[EMHASS](#emhass), and [Predbat](#predbat) below.

**Rate Data Status** reports whether the active horizon is current, outside its
guaranteed lock, illustrative, or incomplete. Its diagnostic attributes expose
the PTO date, export-rate lock end, NBT vintage, tariff effective date and
advice letter, source URL, and `complete` / `exact` / `locked` quality flags.

> Large, frequently-changing attributes (the forecast's `rates` list, and
> Predbat's `raw_today` / `raw_tomorrow` when enabled) are excluded from the
> recorder (`_unrecorded_attributes`). That is deliberate: the coordinator
> recomputes every minute, and writing a 48-hour curve to the database 1,440
> times a day would bloat it for no gain. The current numeric state of every
> sensor is still recorded normally, which is what native History graphs and
> the Energy dashboard read.
>
> Carrying the component breakdown makes `rates` about three times larger —
> roughly 18 KB at the default 48-hour horizon, 64 KB at the 168-hour maximum.
> None of it reaches the database: Home Assistant strips unrecorded attributes
> before it checks its 16 KiB attribute-size limit, so the size warning that
> limit exists to raise cannot fire here. What it does cost is the in-memory
> state and the push to connected dashboards, once an hour when the price
> changes. If you run a long horizon and do not chart the breakdown, lowering
> **Forecast hours** is the lever.

## Component breakdown

A price is not one number; it is a stack of tariff components, and which of
them moved is usually the interesting part. The tariff's own vocabulary is too
fine to chart — fifteen-odd import lines on a bundled schedule, more with a
CCA, and the set changes when a discount or a rate plan does — so TariffKit
rolls those lines up into a fixed set of groups and exposes one sensor per
group per direction.

| Group | Import | Export | What is in it |
|---|---|---|---|
| Generation | ✓ | ✓ | Generation supply from PG&E or a CCA, the PCIA (or the bundled PCIA credit), a CCA cost relief credit, a CCA solar bonus, and a SmartRate event charge |
| Distribution | ✓ | | Distribution, and the Conservation Incentive Adjustment that implements the baseline credit |
| Transmission | ✓ | | Transmission, transmission rate adjustments, reliability services |
| Delivery | | ✓ | The export rate's delivery component, which every Solar Billing Plan customer earns. PG&E does not publish it split into distribution and transmission, so it is one band |
| Surcharges | ✓ | | Public purpose programs, nuclear decommissioning, competition transition, energy cost recovery, wildfire fund and wildfire hardening, new system generation, recovery bonds (charge and its offsetting credit), and the CCA franchise fee surcharge |
| Credits | ✓ | ✓ | CARE, FERA and Medical Baseline discounts; ACC Plus on the export side |
| Other | ✓ | ✓ | Anything TariffKit has not classified. Normally zero — a non-zero reading means a schedule grew a line and the mapping has not caught up |

Two properties make these safe to chart:

* **They sum to the price.** The import groups add up to Import Price and the
  export groups to Export Price, to within per-component rounding. A stack of
  all of them is the price sensor, drawn as its parts.
* **The set never varies.** Every group exists whether or not the account pays
  that kind of charge — a bundled customer's Credits band sits at zero rather
  than the entity disappearing — so a chart written today survives switching to
  a CCA, enrolling in CARE, or a rate change that adds a component.

Each group sensor carries the tariff lines behind it as a `components`
attribute, so the roll-up is auditable from the entity itself:

```yaml
{{ state_attr('sensor.tariffkit_home_import_surcharges', 'components') }}
# {"public_purpose_programs": 0.02644, "wildfire_fund_charge": 0.00595, ...}
```

The same roll-up rides on each hour of the forecast, as `import_components`
and `export_components` on the **Rates Available Through** sensor's `rates`
attribute — grouped rather than per-line, because a 48-hour horizon times
fifteen lines is a lot of attribute for a chart that would draw six bands
anyway.

Grouping is a presentation, not a billing rule. TariffKit's billing ledger
classifies the same components again and differently, for a different
question: which export credits may offset which charges. Neither is derived
from the other.

### A stacked chart

With [apexcharts-card](https://github.com/RomRider/apexcharts-card), each group
becomes one band of a stacked area chart: `stacked: true`, one series per group.

**Use two cards, not one.** Unlike the single-line chart in [History and
forecast timeline](#history-and-forecast-timeline), a stacked chart cannot mix
recorded and forecast series safely. Stacking adds series at each point on the
x axis, and the two halves do not share an x grid: the recorded series are
bucketed by `group_by`, the forecast series land on hour boundaries, and the
forecast's first point is the *start of the current hour* — which is already in
the recorded half. Put them in one stacked card and the current hour is counted
twice. Each half stacks correctly on its own, so give each its own card.

**The recorded past.** Every series is bucketed identically, so the bands line
up. All six bands are listed, including the two that read zero on a bundled
account with no discount: that is what makes the stack equal the price rather
than merely resemble it, and it means the card keeps working when you enroll
in CARE or switch to a CCA.

```yaml
type: custom:apexcharts-card
stacked: true
graph_span: 24h
now:
  show: true
  label: Now
header:
  show: true
  title: Import price by component (recorded)
series:
  - entity: sensor.tariffkit_home_import_generation
    name: Generation
    type: area
    curve: stepline
    extend_to: now
    group_by:
      func: last
      duration: 15min
  - entity: sensor.tariffkit_home_import_distribution
    name: Distribution
    type: area
    curve: stepline
    extend_to: now
    group_by:
      func: last
      duration: 15min
  - entity: sensor.tariffkit_home_import_transmission
    name: Transmission
    type: area
    curve: stepline
    extend_to: now
    group_by:
      func: last
      duration: 15min
  - entity: sensor.tariffkit_home_import_surcharges
    name: Surcharges
    type: area
    curve: stepline
    extend_to: now
    group_by:
      func: last
      duration: 15min
  - entity: sensor.tariffkit_home_import_credits
    name: Credits
    type: area
    curve: stepline
    extend_to: now
    group_by:
      func: last
      duration: 15min
  - entity: sensor.tariffkit_home_import_other
    name: Other
    type: area
    curve: stepline
    extend_to: now
    group_by:
      func: last
      duration: 15min
```

**The forecast.** Every series is read from the same `rates` array, so the
timestamps are identical across bands by construction:

```yaml
type: custom:apexcharts-card
stacked: true
graph_span: 48h
span:
  start: hour
now:
  show: true
  label: Now
header:
  show: true
  title: Import price by component (next 48 hours)
series:
  - entity: sensor.tariffkit_home_rates_available_through
    name: Generation
    type: area
    curve: stepline
    data_generator: |
      return entity.attributes.rates.map((r) => {
        return [new Date(r.start).getTime(), r.import_components.generation];
      });
  - entity: sensor.tariffkit_home_rates_available_through
    name: Distribution
    type: area
    curve: stepline
    data_generator: |
      return entity.attributes.rates.map((r) => {
        return [new Date(r.start).getTime(), r.import_components.distribution];
      });
  - entity: sensor.tariffkit_home_rates_available_through
    name: Transmission
    type: area
    curve: stepline
    data_generator: |
      return entity.attributes.rates.map((r) => {
        return [new Date(r.start).getTime(), r.import_components.transmission];
      });
  - entity: sensor.tariffkit_home_rates_available_through
    name: Surcharges
    type: area
    curve: stepline
    data_generator: |
      return entity.attributes.rates.map((r) => {
        return [new Date(r.start).getTime(), r.import_components.surcharges];
      });
  - entity: sensor.tariffkit_home_rates_available_through
    name: Credits
    type: area
    curve: stepline
    data_generator: |
      return entity.attributes.rates.map((r) => {
        return [new Date(r.start).getTime(), r.import_components.credits];
      });
  - entity: sensor.tariffkit_home_rates_available_through
    name: Other
    type: area
    curve: stepline
    data_generator: |
      return entity.attributes.rates.map((r) => {
        return [new Date(r.start).getTime(), r.import_components.other];
      });
```

Keep `other` on the chart even though it should always read zero. It is where
a tariff line TariffKit has not classified yet would land, and a chart that
omits it would simply stop adding up to the price — quietly, and looking
exactly as it did the day before. That is the failure the band exists to make
visible.

A negative band — a CARE discount, or the bundled PCIA credit inside
Generation — stacks downward, which is the honest picture: it is a number that
reduces the price, and the stack still totals the price.

For the export side, swap the entity prefixes to `export_` and read
`r.export_components.{generation,delivery,credits,other}` — all four bands, for
the same reason. Do not mix the two directions in one stack: they are different
quantities that happen to share a unit, and stacking a credit on top of a
charge totals nothing meaningful.

Native History cards cannot stack, and cannot draw the forecast half at all
(see [History and forecast timeline](#history-and-forecast-timeline)). With
nothing but Home Assistant Core, add the group sensors to a History card and
read them as separate lines.

## Metered energy

Optional. Point TariffKit at the two cumulative kWh counters your meter or
meter reader publishes and it prices what actually moved, not just what a kWh
would have cost:

**Configure → Metered energy** — it is not part of initial setup, so an entry
created before you integrated a meter picks it up later without being
recreated:

| Field | What it is |
|---|---|
| Grid import (energy delivered) | Cumulative kWh taken from the grid. A statement calls this *energy delivered* |
| Grid export (energy received) | Cumulative kWh sent to the grid. A statement calls this *energy received* |
| Billing cycle start day | Fallback only — the day of the month your meter is read. `0` uses the calendar month. Ignored whenever the profile carries statement evidence |

Both entities are optional and independent. Name neither and none of the
running-total entities are created at all. Naming fewer than before removes the
entities that can no longer be answered, rather than leaving them behind as
permanently unavailable.

An account profile imported from the CLI already carries its own
`meter_sources.ha` mapping, so the entities may exist before you ever open this
step — the form shows what it inherited, and clearing a field overrides it. Name only the import counter — a
site with no solar — and the export-credit entities are left out rather than
sitting at a permanent zero: a credit the meters cannot answer for is not a
series with nothing in it.

Having named them, see [Backfilling history](#backfilling-history) to price
whatever the recorder already holds for those entities — the running totals
below only ever compute forward from now.

### The counters do not have to reset

They usually do not. The Rainforest Eagle-100's
`sensor.eagle_100_energy_delivered` and `sensor.eagle_100_energy_received` are
monotonic counters that only ever climb, and today's energy is a *difference*
between two points on one.

TariffKit does that arithmetic out of the recorder's own long-term statistics,
which is where it belongs: a statistic's hourly `change` already absorbs
counter restarts, integration reloads, and the Eagle's meter-session drops.
Statistics compile at the top of the hour, so the hour in progress is read
live off the entity state instead — the last completed hour's recorded value
is a baseline the counter has advanced from. Anything implausible (a negative
change, or more than 100 kW for an hour, which is a restarted statistics
series reporting its whole accumulated total) is dropped with a log line
rather than billed.

Two consequences worth knowing:

- **The recorder is required for this feature.** It is a default integration;
  if you have disabled it, the rate entities carry on untouched and the running
  totals read `unknown` with the reason in their `warnings` attribute.
- **A restart loses nothing.** The totals are re-derived from statistics on
  every refresh, so they survive restarts, reloads, and configuring the
  integration halfway through a day.
- **A gap is reported, not absorbed.** If the recorder was down for part of the
  cycle the energy is genuinely missing, so the figure is understated — the
  entity says so in `warnings` and sets `quality.complete` to false rather than
  presenting a smaller number as if it were finished.

### What the totals mean

Readings are priced by `tariffkit.billing.BillEngine` — the same code that
reconciles against a printed PG&E statement, so a dashboard tile and a
month-end bill cannot drift apart in their arithmetic. That buys time-of-use
bucketing, the Energy Commission Tax, the baseline credit where a schedule has
one, and the rule that exports before Permission To Operate are metered but
earn nothing.

Today's figures are the **cycle's movement across today**, not a one-day bill.
That distinction is not pedantry: parts of a bill are cumulative over a cycle
rather than additive over its days — the baseline allowance most of all, which
is granted per cycle and consumed in day order. Pricing today alone would grant
it a single day's allowance however much the cycle had banked, overstating a
heavy day and letting it cost more than the cycle containing it. Taking the
difference between two cycle-to-date bills has neither problem: the days sum to
the cycle exactly. A single day *can* still exceed its cycle — a heavy import
day inside a month that exports for the rest costs more on its own than the
whole cycle does, because the later days earn credit against it.

- **Energy Cost** is the day's (or cycle's) import charges including statutory
  per-kWh taxes, and excluding the fixed charge.
- **Export Credit** is what the exports earned, as a positive number.
- **Amount Due** is **what a statement would charge**: the charges, **plus the
  whole of the Base Services Charge for each day so far**, less as much carried
  and earned credit as the tariff lets reach them. The fixed charge is incurred
  for the day of service rather than earned by the hour, which is how a
  statement bills it.

Amount Due is deliberately not `energy_cost − export_credit + fixed_charges`.
Under Net Billing a cycle that earns more credit than it owes does not produce a
refund — the excess **banks** and is spent on a later cycle — and a credit may
only offset charges the tariff lets it reach, so Non-Bypassable Charges stay due
however large the bank. Two attributes say where the difference went:

| Attribute | Means |
|---|---|
| `credit_applied` | Credit actually spent against this period's charges |
| `bank_change` | How much the bank moved: earned less applied. **Negative** for any period that spends more than it earns, which is the normal winter case |

It is never negative for a cycle, because a statement charges nothing rather
than paying out. A negative figure for **today** means today's exports offset
charges the cycle had already run up, which is a real marginal contribution
rather than a refund.

Every figure comes from `tariffkit.billing.apply_credits`. The only arithmetic
the integration does is subtracting one library-computed figure from another to
get a day out of two cycle-to-date bills.

One caveat on a CCA account. `apply_credits` is given the merged bill, and the
library documents that as an approximation: the two banks spend in an order a
merged view cannot reproduce, and an exact answer needs each provider's charges
and credits fed separately. It matters only where a scoped credit cap binds,
which no reconciled statement has yet shown — but the entity is closer to "what
a statement would charge" than to "what the statement charged", and on a
bundled account only the second reading is exact.

Every money entity carries its own decomposition as attributes —
`energy_charges`, `taxes`, `export_credits`, `fixed_charges`, `credit_applied`,
`bank_change`, `imported_kwh`, `exported_kwh`, the time-of-use `buckets`,
`quality`, and any pricing `warnings` — so a surprising figure is auditable from
the entity:

```yaml
{{ state_attr('sensor.tariffkit_home_amount_due_today', 'buckets') }}
{{ state_attr('sensor.tariffkit_home_amount_due_cycle', 'warnings') }}
```

The `buckets` on a **today** entity are the cycle's buckets less yesterday's, so
they decompose the figure they are shown beside. Bucket energy and charge
accumulate hour by hour, which makes differencing them exact — unlike the
cycle-cumulative parts the state is careful not to difference.

**When the bank is not applied.** What a cycle owes depends on the credit
carried into it, so the integration refuses to guess: if the export credit bank
has not been folded yet (the first tick after a restart), could not be read, or
is not trustworthy, the figures are stated **before any bank offsets them**, the
reason appears in `warnings`, and `quality.complete` is `false`.

### Where the cycle boundary comes from

A meter-read day is a guess, and usually a wrong one: PG&E reads on business
days, so one real account's cycles opened on the 29th, the 30th, the 1st and
the 3rd in consecutive months. No fixed day of the month matches more than a
fraction of them.

So TariffKit prefers evidence. If the profile carries imported statements — via
[Account history](#account-history), or `tariffkit account sync` / `account
import-statement` on the CLI — the cycle boundary comes from the statements
themselves. Billing periods are contiguous, each beginning the day after the
last one ended, so the *open* cycle's start follows from the most recent
statement without waiting for the one that will close it.

The `cycle_boundary` attribute on the three cycle **money** entities says which was used:

| Value | Means |
|---|---|
| `statement` | A real billing period, exact |
| `day_of_month` | The configured meter-read day; approximate |
| `calendar_month` | No read day configured either; the 1st of the month |

Evidence more than 35 days stale is ignored — a statement has been issued that
the profile never imported, so the next boundary is no longer derivable, and
trusting the old one would report a 90-day "cycle" and bill Base Services
Charge for every day of it. It falls back and says so.

The cycle figures are **cycle to date**, not a balance due. Under Net Billing
an export credit carries into the next cycle and settles at the annual
true-up; that is a stateful ledger
(`tariffkit.billing.ledger`, see [Billing](billing.md)), not something a
running total can show. If your billing cycle start day is left at `0` the
"cycle" is simply the calendar month, which will not line up with a statement.

Grid Import and Grid Export report what the meter saw, named for the
direction rather than for the meter's own vocabulary — a statement calls them
*energy delivered* and *energy received*, which is unambiguous on paper and
undecidable in an entity list. Grid Export's `compensated_kwh` attribute
reports what the tariff will pay for, which is less whenever a site exported
before its PTO date.

## Backfilling history

The running totals compute forward from the moment you name the meters.
Everything before that is unreachable inside Home Assistant even though the
recorder usually holds every reading needed to price it — which is the common
case for anyone who was on the tariff before finding the setting.

**Developer tools → Actions → TariffKit: Backfill metered usage**, or:

```yaml
action: tariffkit.backfill_usage
data:
  config_entry: <your entry>
  # Optional. Defaults to the billing cycle containing your PTO date, which is
  # where bills begin meaning anything: Net Billing compensation runs from
  # Permission To Operate, so an earlier cycle earns nothing however much it
  # exported. An account with no PTO falls back to the profile's first epoch.
  start: "2026-06-03"
response_variable: backfilled
```

The response carries a `cycles` list alongside the daily totals — one entry per
billing cycle priced, with its own charges, taxes, credits and fixed charges,
and three figures that are easy to confuse:

| Field | Means |
|---|---|
| `total` | The bill's own sum, every credit earned subtracted. Goes negative on an exporting cycle; no statement prints this |
| `cash_due` | What the statement charged, after the annual settlements and the bank |
| `bank_closing` | The balance standing *after* that cycle — a running total, not the cycle's own contribution |

Where a day inside a cycle could not be published, `days_unpriced` counts it and
`residual` states exactly how much the cycles hold that the daily rows do not,
so the two figures never differ without saying so. A skipped day still owes its
Base Services Charge, so the residual is more than that day's energy.

The run is folded through **every annual settlement it crosses**, not merely
carried from cycle to cycle: a true-up claws back credit already paid out as Net
Surplus Compensation, so each cycle after an anniversary opens with less bank
than a straight fold would give it. Getting this wrong made the published
history disagree with the live entities by hundreds of dollars.

### Running it the first time, just after setting up

Do this as soon as the meters are configured — you do not have to wait for the
running totals to accumulate anything. If you are about to configure meters on
an instance you also intend to update, see [Upgrading](#upgrading) first: doing
it in that order saves a seam in the recorded history. The action reads the recorder's
statistics for the meter entities, and those usually predate the integration by
months, because the meter sensor existed before TariffKit did.

1. **Set up the account profile and configure the meters** — [Configure](#configure)
   and [Metered energy](#metered-energy). Nothing below works until the meters
   are named.
2. **Find out how far back your meter statistics actually go.** Developer tools →
   Statistics, search for your grid-import entity, and note its earliest date.
   That, not the profile's first epoch, is the real limit on how much history
   can be priced.
3. **Pick a start on or before a billing-cycle boundary that your statistics
   cover** — see the caveat below, which is the one thing likely to catch you
   out.
4. **Run the action** and read the response.

```yaml
action: tariffkit.backfill_usage
data:
  config_entry: <your entry>
  start: "2026-04-30"
response_variable: backfilled
```

5. **Check `skipped`, `warnings` and `complete` before trusting the numbers.**
   `complete: true` with both lists empty means every day in the window was
   priced against a full cycle. Anything else names what was left out and why.

#### Your history has to start at a cycle boundary

A billing cycle can only be decomposed from its own first day, so a cycle the
window joins partway through is refused rather than mispriced. The action
already snaps a start date you give it back to its cycle's beginning — but it
then clips the window forward to your first actual reading, and if *that* lands
mid-cycle the leading cycle still goes.

That is not a failure to work around; it is the action telling you the earliest
date it can honestly price from, and the message says which:

```
skipped:
  2026-07-29..2026-08-23: the window starts inside this cycle (2026-07-31),
  so its days cannot be priced against a full cycle. Backfill from 2026-07-29
  or earlier to include it
```

If your statistics do not reach that boundary, the cycle genuinely cannot be
priced and the next one is where your history begins. Nothing is lost by trying:
a refused cycle costs you a line in `skipped`, not a wrong number.

#### And it should start at the cycle containing your PTO date

Different problem, same window. A backfill opens the export credit bank at zero,
which is only true where compensation began — so a run starting later is missing
every credit earned in between, and overstates every amount due by whatever that
credit would have offset. Nothing is skipped and no day is unpriced; the run
looks clean apart from one line in `warnings`:

```
warnings:
  this run starts at 2026-10-01, after the cycle containing Permission To
  Operate (2026-06-03), so it opens the export credit bank at zero. Any credit
  earned between those dates is missing, and every amount due here is
  overstated by whatever it would have offset. Backfill from 2026-06-03 for a
  bank that carries
```

Leaving `start` unset picks that cycle for you, which is why it is the default.

### Running it again later

Rerunning is the normal way to keep backfilled history honest, and it is safe:
each run replaces the days it covers rather than adding to them, and a run over
a narrower window continues the running total it finds rather than restarting
it. Re-run after any of these:

| After | Because |
|---|---|
| Correcting account history — tariff, supplier, CCA, an epoch date | The corrected settings reprice the whole window. Statistics already written were priced under the old ones and nothing detects that on its own |
| Importing statements (`account sync` / `account import-statement`, then re-importing the profile) | Cycle boundaries become exact instead of falling back to the meter-read day, so the days regroup into the cycles a bill actually used |
| Your meter history growing further back | A wider window prices cycles that were previously out of reach |
| Upgrading TariffKit, when a release changes pricing | The changelog says when this applies |

Nothing is stored about previous runs, deliberately — there is no state that
could go stale, and no bookkeeping to get wrong. The written statistics are the
only record, and rewriting them is the whole update mechanism.

It prices every finished day in the window and writes five **external
statistics** under a `tariffkit:` namespace:

```
tariffkit:<profile>_grid_import     kWh
tariffkit:<profile>_grid_export     kWh
tariffkit:<profile>_energy_cost     USD
tariffkit:<profile>_export_credit   USD
tariffkit:<profile>_amount_due      USD
```

Add them to a **Statistics graph** card, or to the Energy dashboard, the same
way any long-term statistic is used. They are deliberately *not* written into
the running-total entities' own series: a separate namespace has no seam to
reconcile with what the live path is writing, and it leaves the recorder's own
hourly compilation of those entities entirely alone. This is the shape Home
Assistant's own `opower` integration uses to publish utility history.

### What it will and will not tell you

**One row per finished day, not per hour.** A bill is a daily and cyclical
artefact: the Base Services Charge is per day, the energy surcharge is floored
per day, and the baseline allowance is granted per cycle and consumed in day
order. Only the energy charges and export credits are additive per hour, so an
hourly series would have to invent an attribution for everything else. A day is
the finest slice this can state exactly.

**Each day is its cycle's movement across that day**, computed by differencing
consecutive cycle-to-date bills — the same decomposition the Today entities
use, and for the same reason. The days sum to their cycle exactly. They are not
individually bounded by it: a heavy import day inside a month that exports for
the rest can cost more on its own than the whole cycle does, because the later
days earn credit against it.

One attribution caveat. A SmartRate credit is earned against the cycle's whole
eligible usage but falls due on the event day, so differencing books it entirely
onto that day. The cycle total stays exact; the day it lands on reads a few
dollars low and the days before it read correspondingly high.

**The three dollar series do not reconcile with each other.** `energy_cost` is
import charges plus taxes; `amount_due` adds the Base Services Charge and then
applies only as much credit as the tariff permits, banking the rest. So
`amount_due` is not `energy_cost − export_credit` plus the daily charge — on an
exporting account it is higher, by whatever banked. That is not an error: it is
the same distinction a statement draws between the credit you earned and the
credit you got to spend, and it is why the cycle summary reports `total`,
`cash_due` and `bank_closing` separately.

**Start at the PTO cycle unless you know better.** A backfill opens the export
credit bank at zero, which is only true where compensation began. Started later
— re-running a short window after fixing a meter, say — every credit earned in
between is missing and every amount due in the window is overstated by whatever
it would have offset. The response warns when a run does this; the fix is to
backfill from the date it names.

**Today is excluded.** The window ends at yesterday; today is what the running
totals are for.

**Rerunning replaces, it does not append.** So run it again after correcting
account history — the corrected settings reprice the whole window. This is also
why nothing is stored about previous runs: there is no state to go stale. A
rerun over a *narrower* window is safe too: the running total continues from
whatever the series already held before the window, rather than restarting.

**Only days the recorder can actually account for are priced.** That holds at
the window's edges *and* inside it: a start date earlier than your meter sensor
existed does not manufacture months of daily charges, and a recorder outage in
the middle leaves those days unpriced rather than billing them as zero-usage
days. The day the recorder *returns* on is left out too: a counter that was
unreachable reports its whole catch-up in the first hour it is seen again, and
that hour cannot be separated from the day's own usage. The kWh survives — a
cumulative counter depends only on its endpoints — but the time-of-use shape
does not, and the shape is what the tariff prices. Each omission is reported in
`warnings`, and `complete` in the response is false whenever anything was
skipped or warned about.

Coverage is judged **per meter**, not on the two directions combined. Import
statistics with no export statistics would otherwise look like a site that
simply never exported, and every credit it earned would vanish silently; instead
the missing series is named in `warnings`.

**Days are labelled in Pacific time.** On an instance more than seven hours west
of Pacific the day boundaries shift by one; everywhere else they line up.

**Renaming a profile starts a new set of series.** The old
`tariffkit:<oldname>_*` statistics remain and are not cleaned up; delete them
under **Developer tools → Statistics** if you do not want them.

**A cycle that cannot be covered in full is skipped whole**, and named in the
response's `skipped` list — whether the account history begins inside it or the
requested window does. The days after the epoch have nothing to be
marginal *to*, and pricing them as though the cycle began at the epoch would
under-grant a baseline allowance the real cycle had been banking since its true
start. Backdating the profile's first epoch is the fix; see
[Account history](#account-history).

The response reports what was written:

```yaml
days: 83
first_day: "2026-06-03"
last_day: "2026-08-24"
grid_import_kwh: 412.881
grid_export_kwh: 1974.2
energy_cost: 88.41
export_credit: 731.05
# What the published days sum to: what those cycles actually charged, with the
# credit they could not spend carried into the bank rather than refunded.
amount_due: 154.98
residual: 0.0
cycles:
  - start: "2026-06-03"
    end: "2026-06-29"
    total: -119.60        # the bill's own sum, every credit subtracted
    cash_due: 25.27       # what the statement charged
    credit_applied: 9.04  # what the charges could absorb
    bank_closing: 144.87  # the balance standing after this cycle
    complete: true
skipped: []
warnings: []
```

## The export credit bank

Under Net Billing an export credit does not settle at the end of the cycle that
earned it. It banks, offsets later cycles' charges, and carries across the
annual true-up — which does not zero it either; a true-up claws back only what
Net Surplus Compensation already paid for. The bank is therefore the number that
answers "what has the solar actually done", and it is not a figure any single
statement prints.

### There are two banks, not one

Where a Community Choice Aggregator supplies your generation, the credits are
kept by two different parties on two unrelated calendars. A statement prints
them on separate pages — PG&E's *Energy Delivered Credits* and *Bonus Credits*
against the CCA's *Energy Export Credit* — and they settle independently: PG&E
at your Permission To Operate anniversary, the CCA on its own cash-out year.
Adding them together gives a figure no statement shows and that never settles as
a whole.

So there are two entities:

| Entity | Holds |
|---|---|
| **Export credit bank (utility)** | The delivery and bonus buckets — PG&E's |
| **Export credit bank (generation)** | The generation bucket — your CCA's |

On a bundled account PG&E supplies generation too, so all three buckets are its
own. Only the first entity exists there — two entities reporting one balance
under names that read as complementary halves is an invitation to add them and
double it.

Both appear whenever a grid-export meter is configured. Their attributes carry
the split the tariff keeps, and enough context to judge the figure:

| Attribute | Means |
|---|---|
| `generation`, `delivery`, `bonus` | The bank by bucket. Credits are spent against matching charges, so the split is not cosmetic |
| `cycles`, `from`, `through` | How many billing cycles were folded, and over what span |
| `true_ups` | Annual events crossed. Empty in a first year |
| `split_between_suppliers` | True when a CCA supplies generation, which is what makes this two banks |
| `credit_cap_verified` | Always false today. The library has not reconciled the credit cap against a statement, and a non-zero bank is exactly the case that would |

A run that spans a change of generation supplier is reported as untrustworthy.
An annual settlement settles a *year*, and there is no way to say that year was
half one arrangement and half another — so the balance is folded under one of
them, and saying so is the only honest option.
| `warnings`, `quality.complete` | Whether the balance can be trusted at all |

### What it is a balance *of*

**Closed cycles only.** Credits apply when a cycle closes, so between closes the
bank sits still at the last closing balance. What the open cycle has earned so
far is a different number, and the **Export credit this cycle** entity already
carries it. A projected balance combining the two would read better and would be
a figure no statement will ever show.

**It needs an unbroken run of cycles**, and says so when it does not have one.
`run_ledger` in the library deliberately does not check — "a ledger over a
discontinuous run is the caller's business" — so this checks. Folding across a
missing cycle does not merely lose that cycle: the credits it earned and spent
are absent from the arithmetic entirely, so the balance reported never existed.
A gap, or a cycle priced from incomplete rates, clears `quality.complete` and
names itself in `warnings`.

**It opens at zero, by construction.** The fold starts at the billing cycle
containing your PTO date. Nothing before Permission To Operate earns anything,
so there is no earlier balance to carry and no opening figure anyone would have
to supply — see [Backfilling history](#backfilling-history), which starts in the
same place for the same reason.

**A rollover is the moment it is most likely to be wrong.** A cycle's final
hour is compiled by the recorder shortly *after* midnight has already opened the
next cycle, so a fold done at that instant can be short of an hour — and a
missing hour at the *end* of a window is not a gap between readings, so nothing
in the series reveals it. The per-meter coverage check does, and an untrustworthy
balance is refolded hourly through the first day of a new cycle rather than
being cached wrong for a month.

**It is recomputed, never accumulated.** The whole run is priced again whenever
a cycle closes, so correcting account history or importing statements fixes the
bank on the next cycle rather than leaving a stored balance quietly wrong. That
costs a months-long recorder read and a second or two of pricing, which is why
it happens once per cycle and not on the minute tick.

**A gap costs a bank more than it costs a day, and it is not fully avoided.** A
cycle's energy survives a recorder outage exactly — a cumulative counter depends
only on its endpoints — but its dollar value moves with time-of-use shape,
because a counter catching up reports the whole outage in the hour it returns.
Days that cannot be accounted for are left unpriced, so they never reach the
daily statistics; the cycle bill the bank folds still contains their energy,
priced at whatever hour the catch-up landed in. The balance is flagged when that
happens — `quality.complete` goes false and a warning names it — but it is
reported with a caveat rather than withheld, because the alternative is
discarding a whole cycle over one hour.

### On a CCA account

If a Community Choice Aggregator supplies your generation, PG&E's annual true-up
settles nothing in cash — the bank carries forward and PG&E pays no Net Surplus
Compensation, under Special Condition 5.a. Your CCA's own cash-out is a separate
event on its own calendar. A bundled account faces the surplus test instead. The
`true_ups` attribute names whichever events the folded run has actually crossed,
and stays empty for a year that has not closed rather than implying it settled
at zero.

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
entirely in the **Rates Available Through** sensor's unrecorded `rates`
attribute: a list of `{start, end, import, export, spread, import_components,
export_components}` points reaching from now out to the account's configured
forecast horizon. The two `*_components` maps are the grouped breakdown from
[Component breakdown](#component-breakdown), which is what a stacked forecast
chart is drawn from.

**Fallback with no custom card:** add Import Price, Export Price, and
Export Minus Import to a native **History** graph card for the live,
recorded past, and add **Rates Available Through** to an **Entities** card so
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
  - entity: sensor.tariffkit_home_rates_available_through
    name: Import (forecast)
    color: "#b87333"
    stroke_dash: 6
    curve: stepline
    data_generator: |
      return entity.attributes.rates.map((r) => {
        return [new Date(r.start).getTime(), r.import];
      });
  - entity: sensor.tariffkit_home_rates_available_through
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
Export Minus Import, styled the same way, if you want it on the same axis.
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
       "utility": "pacific_gas_and_electric", "tariff": "E-ELEC", "account_profile": "home",
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
       "utility": "pacific_gas_and_electric", "tariff": "E-ELEC", "account_profile": "home",
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

> For a step-by-step guide to installing and wiring both sides together —
> including Predbat's own setup, verification, and troubleshooting — see
> [Predbat](predbat.md). This section covers what the integration publishes.

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
to backfill a partial day by copying the previous one. Each entry uses
Predbat's `from` / `to` / `rate` shape; `rate` is already expressed in cents,
so Predbat does not apply another 100x currency conversion.

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
Assistant's configured time zone, and today's Pacific calendar date. With
[Metered energy](#metered-energy) configured it also carries whether each
direction is set, the billing cycle start day, and the day's and cycle's
computed bills — including how many hours the recorder supplied.

It deliberately omits the account's full profile, its observations, and any
meter-source mapping — nothing that could identify the account or reveal how
its bill has been reconciled, only what a report needs to say "here is which
rate rules were active and how trustworthy they are." The metered-energy block
follows the same rule: it reports *that* an import or export entity is
configured, never which entity, and its bills carry no entity names.

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
          {% set points = state_attr('sensor.tariffkit_home_rates_available_through', 'rates') %}
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
