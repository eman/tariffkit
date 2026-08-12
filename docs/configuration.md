# Configuration

Every entry point (library, CLI, MQTT, web, Home Assistant) prices from the
same `Config` object. Get this right once and all of them agree.

## Where settings come from

`Config.load()` resolves in this order, later winning:

1. Built-in defaults (PG&E bundled, NBT26, PTO 2026-06-03).
2. `~/.config/nem-rates/config.toml`, or `$XDG_CONFIG_HOME/nem-rates/config.toml`.
3. An explicit `--config /path/to.toml`.
4. `NEM_RATES_*` environment variables.

Check what actually resolved before trusting a number:

```bash
nem-rates info
```

## A worked example: PG&E delivery + MCE generation

```toml
# ~/.config/nem-rates/config.toml
supplier = "cca"
interconnection_year = 2026        # -> NBT26 vintage, ACC Plus $0.00880/kWh
pto_date = "2026-06-03"            # -> 9-year lock ends 2035-06-02
acc_plus_segment = "residential"
base_services_charge_tier = 3      # 1 = CARE, 2 = FERA, 3 = standard

[cca]
name = "MCE"
rate_card = "mce"                  # vendored; see below
option = "light_green"             # or "deep_green" (+$0.0125/kWh)
pcia_vintage = 2011                # the year your bill names; see below
```

Bundled PG&E service needs no config file at all.

## Settings

| Key | Values | Notes |
|---|---|---|
| `tariff` | `E-ELEC`, `E-TOU-C`, `EV2-A` | Defaults to `E-ELEC` |
| `supplier` | `bundled`, `cca` | `cca` requires a `[cca]` table |
| `interconnection_year` | 2023–2026 | Picks the NBT vintage and ACC Plus rate |
| `pto_date` | ISO date | Starts the nine-year rate lock |
| `vintage` | `NBT23`…`NBT26`, `NBT00` | Overrides `interconnection_year` |
| `acc_plus_segment` | `residential`, `residential_low_income`, `none` | |
| `discount` | `none`, `care`, `fera` | Requires `acc_plus_segment = "residential_low_income"` |
| `base_services_charge_tier` | 1, 2, 3 | $/day, reported separately from $/kWh |
| `baseline_territory` | `P`…`Z` | E-TOU-C only; your bill names it |
| `baseline_code` | `basic`, `all_electric` | PG&E's Code B / Code H |

`[cca]` keys: `name`, `rate_card`, `option`, `pcia_rate`, `pcia_vintage`,
`franchise_fee_surcharge`, `generation_rates`, `export_generation_rate`.

### PCIA and the franchise fee surcharge

Set **`pcia_vintage`** and both are handled. They are vintaged off the same
year, and the published tables for 2009–2026 are vendored: the PCIA from
Schedule E-ELEC Sheet 5, the franchise fee from Schedule E-FFS.

Your bill names the vintage. Look for a line like *"2011 Vintaged Power Charge
Indifference Adjustment"* under the Solar Billing Plan or electric delivery
detail.

`pcia_rate` and `franchise_fee_surcharge` still exist and still take precedence,
for a vintage that is not vendored (the sheet's "Pre-2009" bucket, or a year
newer than the vendored sheet). Prefer the vintage: a rate reverse-engineered
from a billed dollar amount inherits that amount's rounding, which on a small
bill can be several percent.

## Rate schedules

Three residential schedules are vendored. Adding another is a data change, not a
code change: drop a dated snapshot under
`src/nem_rates/data/tariff/pge/<slug>/`.

| Schedule | Periods | Baseline |
|---|---|---|
| `E-ELEC` | peak 4–9pm, part-peak 3–4pm and 9pm–12am, off-peak otherwise | no |
| `EV2-A` | same shape as E-ELEC, different rates | no |
| `E-TOU-C` | peak 4–9pm, **no part-peak** — off-peak otherwise | yes |

All three apply the same periods every day of the week, holidays included, and
share the June–September summer season.

### The E-TOU-C baseline credit

E-TOU-C credits $0.08140/kWh on usage within a baseline allowance. That is a
*quantity*, not a time, so no marginal price can express it:

```python
price = engine.price_at(moment).import_price
price.total  # the over-baseline price
price.baseline_credit  # 0.08140; subtract for a kWh still inside the allowance
```

`price_at` deliberately returns the over-baseline price, which is the right
answer for a dispatch decision — an allowance is normally spent early in the
cycle, so the next kWh is over it. The billing engine sees a whole cycle and
applies the credit itself, as a `baseline_credit` line:

```toml
tariff = "E-TOU-C"
baseline_territory = "X"      # your bill: "Baseline Territory X"
baseline_code = "basic"       # or "all_electric" if space heating is electric
```

The allowance is a daily quantity that changes at the season boundary, so it
accumulates day by day rather than being multiplied by the cycle length —
territory P all-electric is 15.2 kWh/day in summer against 26.0 in winter.

**Without `baseline_territory` there is no credit line at all.** The quantities
vary several-fold between territories, so guessing one would be worse than
reporting none.

## Home Assistant

Only needed for `nem-rates bill --source ha`. Entity ids are configuration and
live in the config file; the access token is not and does not.

```toml
[home_assistant]
host = "https://homeassistant.example:8123"
import_entity = "sensor.eagle_100_energy_delivered"
export_entity = "sensor.eagle_100_energy_received"
```

Both entities default to the Rainforest Eagle-100 pair above, so a `[home_assistant]`
section is only needed to point elsewhere. Note the defaults are the
**monotonic-filtered** entities — the similarly named
`sensor.eagle_100_total_energy_delivered` is the raw device feed and drops to
zero several times a day when the meter session restarts.

Credentials come from `.env` in the working directory, or the environment.
The file is not shell — spaces around `=` and quoted values are fine, and are
what the parser expects:

```ini
# .env
HA_HOST = "https://homeassistant.example:8123"
HA_TOKEN = "<long-lived access token>"
```

To use the environment instead, export them as shell variables:

```bash
export HA_HOST=https://homeassistant.example:8123
export HA_TOKEN=...
```

Resolution order, later winning: the config file, then `.env`, then real
environment variables, then `--ha-import-entity` / `--ha-export-entity`.
`HA_TOKEN` is deliberately never read from the config file, which is not
gitignored.

## Environment variables

`NEM_RATES_SUPPLIER`, `NEM_RATES_VINTAGE`, `NEM_RATES_INTERCONNECTION_YEAR`,
`NEM_RATES_PTO_DATE`, `NEM_RATES_ACC_PLUS_SEGMENT`, `NEM_RATES_DISCOUNT`,
`NEM_RATES_BSC_TIER`.

**These do not cover `[cca]` settings.** A container or service that needs CCA
pricing must mount a config file; setting `NEM_RATES_SUPPLIER=cca` alone raises
`ConfigError` because no `CcaConfig` is supplied.

## CCA service

Under NEM 3.0 a CCA customer receives only the **delivery** component of the
export credit from PG&E. Generation compensation comes from the CCA, and the
two halves of your import price come from two different bills.

One rate card is vendored:

| Provider | `rate_card` | Covers |
|---|---|---|
| MCE (Marin Clean Energy) | `"mce"` | ELEC generation by season/period, Cost Relief Credit (expires 2026-12-31), Deep Green premium, 10% Solar Bonus Credit |

For any other CCA, supply rates directly:

```toml
[cca]
name = "Ava Community Energy"
pcia_vintage = 2011                # covers the franchise fee surcharge too

[cca.generation_rates.summer]
peak = 0.26299
part_peak = 0.16388
off_peak = 0.11878

[cca.generation_rates.winter]
peak = 0.10086
part_peak = 0.08089
off_peak = 0.06754
```

Until generation rates and a franchise fee are supplied, CCA mode returns
delivery-only prices flagged `complete = False` rather than a plausible-looking
wrong total. Check that flag before acting on a price. Setting `pcia_vintage`
satisfies the franchise fee half on its own.

## Reading your bill

PG&E splits a CCA customer's charges across lines that individually look like
the answer and are not:

- **"Energy Produced"** is PG&E's own generation rate, cancelled in full by the
  "Generation Credit" line directly beneath it. You do not pay it.
- **"Energy Delivered"** excludes Non-Bypassable Charges, which are a separate
  per-kWh line. Delivery is the sum of the two.
- **PCIA** and **Franchise Fee Surcharge** print a dollar amount with no rate.
  Divide by the billed kWh to get $/kWh, and note the basis is not stated on
  the bill; treat the result as derived.

Marginal cost of an imported kWh is the sum of every per-kWh line across both
bills. The Base Services Charge is $/day and is deliberately excluded.

## Verifying against a bill

```python
from datetime import datetime
from nem_rates import Config, PACIFIC
from nem_rates.tariff.retail import RetailTariff

tariff = RetailTariff(Config.load())
for label, hour, kwh in [("off_peak", 12, 22.903), ("peak", 17, 0.458)]:
    rate = tariff.price_at(datetime(2026, 7, 29, hour, tzinfo=PACIFIC)).total
    print(f"{label}: {kwh} kWh @ ${rate:.5f} = ${kwh * rate:.2f}")
```

Compare the total against the sum of the per-kWh lines on both bills. They
should agree to within the bill's per-line rounding.
