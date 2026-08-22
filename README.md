<p align="center">
  <img src="https://raw.githubusercontent.com/eman/tariffkit/main/images/banner.svg" alt="TariffKit — electricity pricing, billing, and energy integrations" width="100%">
</p>

# TariffKit

[![CI](https://github.com/eman/tariffkit/actions/workflows/ci.yml/badge.svg)](https://github.com/eman/tariffkit/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/tariffkit)](https://pypi.org/project/tariffkit/)
[![Python 3.14.2](https://img.shields.io/badge/python-3.14.2-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

An offline electricity tariff engine for pricing, billing, and energy-system
integrations. The first data provider supports PG&E residential rate plans under
**NEM 3.0 / the Net Billing Tariff (NBT)**; the package identity is deliberately
not tied to one utility or tariff program.

The complete active single-family residential portfolio is vendored:
**E-1**, **E-ELEC**, **E-TOU-C**, **E-TOU-D**, and **EV2-A**. CARE, FERA,
Medical Baseline/D-MEDICAL, and event-injected SmartRate adjustments are
modeled separately from their underlying plans.

Under NBT your export credit is not a time-of-use schedule: it is an hourly
Avoided Cost Calculator value that swings from about $0.06/kWh at midday to
about $1.19/kWh on an August evening. Knowing what a kWh is worth right now, and
what it will be worth over the next two days, is the input to every useful
solar-and-battery dispatch decision.

```python
from tariffkit import RateEngine

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
component. `tariffkit` collapses it at build time, verifying losslessness cell by
cell, so the entire five-vintage dataset ships inside the wheel at **268 KiB**
and every lookup is a few list indexes.

The retail side is similar: generated effective-dated tables hold every active
schedule. E-TOU-D additionally selects weekday versus weekend/observed-holiday
periods from the vendored tariff calendar.

Nothing here touches the network at runtime.

## Documentation

| | |
|---|---|
| [Configuration](docs/configuration.md) | Settings, CCA setup, reading your bill |
| [Library](docs/library.md) | Embedding in Python |
| [Named account profiles](docs/accounts.md) | Tracking a changing service agreement over time, importing PG&E statements |
| [Bill calculator](docs/billing.md) | Computing a cycle from interval meter data |
| [MQTT](docs/mqtt.md) | Publishing, with Home Assistant discovery |
| [REST API](docs/web.md) | HTTP service |
| [Home Assistant](docs/home-assistant.md) | Custom component, Energy dashboard, stacked component charts, account history, response actions, opt-in Predbat |
| [Predbat](docs/predbat.md) | Installing TariffKit and Predbat together, end to end |
| [Predbat on Sigenergy](docs/predbat-sigenergy.md) | Sigenergy SigenStor specifics: entity mapping, sign and unit conversions, control |
| [Home Assistant quality checklist](docs/home-assistant-quality.md) | Self-assessment against the Integration Quality Scale, with exemptions |
| [Containers](docs/containers.md) | Local Home Assistant development stack and API/MQTT deployment proposal |
| [Maintaining rate data](docs/data.md) | Regenerating export rates, updating the retail tariff and CCA cards |
| [Packaging strategy](docs/packaging_strategy.md) | Architecture decision, boundaries, and release model |
| [Release procedure](docs/releases.md) | Versioning, Trusted Publishing, verification, and recovery |

## Works with

Prices are published in the shapes these already read, via either the custom
component or the MQTT publisher — no template plumbing on your side. The two
surfaces differ for EMHASS and Predbat: MQTT always publishes their
attributes, while the custom component asks for a window on demand and keeps
Predbat opt-in.

| | Custom component | MQTT |
|---|---|---|
| **Home Assistant Energy dashboard** | Import/export price entities | Import/export price entities |
| **EMHASS** | `tariffkit.get_emhass_forecast` action, called with any window | `load_cost_forecast` / `prod_price_forecast` attributes, always published |
| **Predbat** | `raw_today` / `raw_tomorrow` attributes, only once enabled in options | `raw_today` / `raw_tomorrow` attributes, always published |

See [docs/home-assistant.md](docs/home-assistant.md) and
[docs/mqtt.md](docs/mqtt.md) for setup of each.

## Install

```bash
pip install tariffkit              # core, zero dependencies
pip install 'tariffkit[mqtt]'      # + MQTT publisher with Home Assistant discovery
pip install 'tariffkit[web]'       # + FastAPI service
pip install 'tariffkit[secrets]'   # + OS keyring credential storage
pip install 'tariffkit[statements]' # + reading local PG&E statement PDFs
pip install 'tariffkit[all]'
```

The Home Assistant integration has been
[submitted to the default HACS store](https://github.com/hacs/default/pull/10019),
but approval queues can take months. Until it is approved, install TariffKit as
a custom repository:

[![Open your Home Assistant instance and add the TariffKit repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=eman&repository=tariffkit&category=integration)

Or add it manually:

1. Open **HACS** in Home Assistant.
2. Select the **three-dot menu → Custom repositories**.
3. Enter `https://github.com/eman/tariffkit`, choose **Integration**, and select
   **Add**.
4. Search HACS for **TariffKit**, open it, and select **Download**.
5. Restart Home Assistant, then add TariffKit from
   **Settings → Devices & services → Add integration**.

## CLI

```bash
tariffkit now                          # current import/export price
tariffkit forecast --hours 48          # the upcoming curve
tariffkit forecast --format json       # machine-readable
tariffkit mqtt --broker 192.168.1.100  # publish hourly, with HA discovery
tariffkit serve                        # REST API on :8000
tariffkit bill intervals.csv           # compute a cycle from meter data
tariffkit info                         # which data is loaded, and from where
tariffkit account init home            # track a service agreement's history
tariffkit account source home show ha  # inspect profile grid-import/export entities
```

## Configuration

Defaults target a PG&E-bundled residential customer. Point it at your own
service agreement via `~/.config/tariffkit/config.toml`:

```toml
supplier = "bundled"              # or "cca"
interconnection_year = 2026       # selects the NBT vintage and ACC Plus row
pto_date = "2026-06-03"           # starts the nine-year rate lock
acc_plus_segment = "residential"
base_services_charge_tier = 3
```

`TARIFFKIT_*` environment variables override any file setting.
Long-lived PG&E, Home Assistant, InfluxDB, and MQTT credentials can be stored
outside that file with `tariffkit credentials set`; see
[Configuration](docs/configuration.md#credentials).

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
`tools.regen`. Nothing is hand-transcribed and nothing is hand-edited.

```bash
python -m tools.regen                              # rebuild every dataset
python -m tools.regen --check                      # exit 1 if a publisher moved
python -m tools.regen tariff --for-date 2025-12-15 # rebuild a superseded vintage
python -m tools.regen.export --download            # the 843 MB export-rate archive
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
