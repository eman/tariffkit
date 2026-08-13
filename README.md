# nem-rates

Real-time and forecast electricity **import** and **export** prices for PG&E
residential rate plans under **NEM 3.0 / the Net Billing Tariff (NBT)**.

Three schedules are vendored: **E-ELEC** (Electric Home), **E-TOU-C**
(Time-of-Use, peak 4–9 p.m. every day), and **EV2-A** (Home Charging).

Under NBT your export credit is not a time-of-use schedule: it is an hourly
Avoided Cost Calculator value that swings from about $0.06/kWh at midday to
about $1.19/kWh on an August evening. Knowing what a kWh is worth right now, and
what it will be worth over the next two days, is the input to every useful
solar-and-battery dispatch decision.

```python
from nem_rates import RateEngine

engine = RateEngine()
point = engine.price_now()

print(point.import_price.total)  # $/kWh to draw from the grid
print(point.export_price.total)  # $/kWh earned by exporting
print(point.spread)  # positive => exporting beats self-consuming

curve = engine.forecast(hours=48)
for hour in curve.best_export_hours(3):
    print(hour.start, hour.export_price.total)
```

## Why it works offline

PG&E publishes 20 years of hourly export rates per vintage, as CPUC Resolution
E-5301 requires (roughly 40 MB of CSV per vintage). But that file is a lossless
expansion of a 576-cell matrix per year (12 months × 2 day types × 24 hours) per
component. `nem-rates` collapses it at build time, verifying losslessness cell by
cell, so the entire five-vintage dataset ships inside the wheel at **268 KiB**
and every lookup is a few list indexes.

The retail side is similar: these schedules' period boundaries are identical every day
of the week including holidays and do not shift by season, so an import price is
fully determined by `(season, hour)`.

Nothing here touches the network at runtime.

## Documentation

| | |
|---|---|
| [Configuration](docs/configuration.md) | Settings, CCA setup, reading your bill |
| [Library](docs/library.md) | Embedding in Python |
| [Bill calculator](docs/billing.md) | Computing a cycle from interval meter data |
| [MQTT](docs/mqtt.md) | Publishing, with Home Assistant discovery |
| [REST API](docs/web.md) | HTTP service |
| [Home Assistant](docs/home-assistant.md) | Custom component, Energy dashboard, EMHASS, Predbat |
| [Maintaining rate data](docs/data.md) | Regenerating export rates, updating the retail tariff and CCA cards |

## Works with

Prices are published in the shapes these already read, via either the custom
component or the MQTT publisher — no template plumbing on your side.

| | How |
|---|---|
| **Home Assistant Energy dashboard** | Import and export price entities, for grid consumption and return-to-grid compensation |
| **EMHASS** | `load_cost_forecast` / `prod_price_forecast` attributes, timestamped and in dollars |
| **Predbat** | `raw_today` / `raw_tomorrow` attributes, 30-minute slots in cents |

See [docs/home-assistant.md](docs/home-assistant.md) for setup of each.

## Install

```bash
pip install nem-rates              # core, zero dependencies
pip install 'nem-rates[mqtt]'      # + MQTT publisher with Home Assistant discovery
pip install 'nem-rates[web]'       # + FastAPI service
pip install 'nem-rates[all]'
```

## CLI

```bash
nem-rates now                          # current import/export price
nem-rates forecast --hours 48          # the upcoming curve
nem-rates forecast --format json       # machine-readable
nem-rates mqtt --broker 192.168.1.100  # publish hourly, with HA discovery
nem-rates serve                        # REST API on :8000
nem-rates bill intervals.csv           # compute a cycle from meter data
nem-rates info                         # which data is loaded, and from where
```

## Configuration

Defaults target a PG&E-bundled residential customer. Point it at your own
service agreement via `~/.config/nem-rates/config.toml`:

```toml
supplier = "bundled"              # or "cca"
interconnection_year = 2026       # selects the NBT vintage and ACC Plus row
pto_date = "2026-06-03"           # starts the nine-year rate lock
acc_plus_segment = "residential"
base_services_charge_tier = 3
```

`NEM_RATES_*` environment variables override any file setting.

### What the numbers include

- **Import price** is the marginal per-kWh cost: generation + distribution for
  the season and period, plus the flat riders. The AB 205 Base Services Charge
  is a fixed $/day amount and is deliberately *excluded*: folding it into a
  $/kWh figure would corrupt any marginal dispatch decision. Read it separately
  via `engine.daily_fixed_charge()`.
- **Export credit** is the generation component plus the delivery component,
  plus your ACC Plus adder. Values past your nine-year lock are still returned
  but flagged `locked=False`; PG&E publishes them for illustration only.

### Edge cases in PG&E's published data

Found by round-tripping the vendored matrices against all 1.75 million source
rows. The library handles each; they are documented because they are surprising.

- **The autumn DST hour.** The fall-back day has 25 real hours but only 24 rate
  labels. PG&E gives the repeated 01:00 PST the `HS2` label, so it is priced as
  2am. Pricing it by wall-clock hour returns the wrong value for that hour.
- **Far-future drift.** From 2036 onward PG&E's own hour labels stop tracking
  Pacific daylight time, and NBT25/26/00 duplicate some holidays onto the
  following day. Each vintage records the last year verified exact; prices past
  it are returned with `exact=False`. Every year inside a nine-year rate lock is
  exact, and those years are already published as illustrative only.
- **Vintages disagree about holidays** in those same late years, so the holiday
  calendar used for a lookup is the one embedded in that vintage's own file.
- **Only the June 2026 tariff sheets are vendored**, for each of the three
  schedules. Earlier timestamps raise rather than silently back-dating current
  rates onto an older billing period.
- **E-TOU-C's baseline credit is not in the marginal price.** It applies to the
  first N kWh of a cycle, which is a quantity rather than a time, so `price_at`
  reports it as `baseline_credit` and the billing engine applies it.

### CCA customers

If a Community Choice Aggregator supplies your generation, PG&E still delivers,
and under NEM 3.0 you receive **only the delivery component** of the export
credit from PG&E; generation compensation comes from the CCA.

An MCE rate card is vendored (generation by season/period, the Cost Relief
Credit, Deep Green premium, and the 10% Solar Bonus Credit):

```toml
supplier = "cca"

[cca]
name = "MCE"
rate_card = "mce"
pcia_rate = 0.03476                # $/kWh, from your bill
franchise_fee_surcharge = 0.00042  # $/kWh, from your bill
```

For other CCAs, supply `generation_rates` directly; see
[docs/configuration.md](docs/configuration.md). Until generation rates and a
franchise fee are configured, CCA mode returns delivery-only prices flagged
`complete = False` rather than quietly understating your rates.

Note that a CCA customer's PCIA is a **charge**, while a bundled customer's is a
**credit**, so CCA service can cost several cents per kWh more on import.

## Keeping rates current

Every vendored dataset is regenerated from the document that publishes it, by
`nem_rates.regen`. Nothing is hand-transcribed and nothing is hand-edited.

```bash
nem-rates regen                                # rebuild every dataset
nem-rates regen --check                        # exit 1 if a publisher moved
nem-rates regen tariff --for-date 2025-12-15   # rebuild a superseded vintage
python -m nem_rates.regen.export --download    # the 843 MB export-rate archive
```

Export matrices come from PG&E's CSV archive, which is large enough to have its
own entry point; everything else comes from a published PDF. Nothing is written
unless the rendered file survives being read back by the library code that will
consume it, so a generator that drifts from the schema fails instead of shipping.

A weekly CI job runs `--check`, so a rate change surfaces as a failing build
rather than as silent drift. Export files are updated by **October 1** of any
year the CPUC adopts a new Avoided Cost Calculator; retail rates change more
often, via advice letters — three times in the first half of 2026 alone — which
is why a superseded vintage can be rebuilt from the filing that adopted it. See
[docs/data.md](docs/data.md).

## Data sources

See [docs/data.md](docs/data.md#data-sources) for the source of every vendored
table and why OpenEI's URDB is deliberately not used.

## License

MIT

