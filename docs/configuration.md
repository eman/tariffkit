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
pcia_rate = 0.03476                # $/kWh, from your bill
franchise_fee_surcharge = 0.00042  # $/kWh, from your bill
```

Bundled PG&E service needs no config file at all.

## Settings

| Key | Values | Notes |
|---|---|---|
| `supplier` | `bundled`, `cca` | `cca` requires a `[cca]` table |
| `interconnection_year` | 2023–2026 | Picks the NBT vintage and ACC Plus rate |
| `pto_date` | ISO date | Starts the nine-year rate lock |
| `vintage` | `NBT23`…`NBT26`, `NBT00` | Overrides `interconnection_year` |
| `acc_plus_segment` | `residential`, `residential_low_income`, `none` | |
| `discount` | `none`, `care`, `fera` | Requires `acc_plus_segment = "residential_low_income"` |
| `base_services_charge_tier` | 1, 2, 3 | $/day, reported separately from $/kWh |

`[cca]` keys: `name`, `rate_card`, `option`, `pcia_rate`, `pcia_vintage`,
`franchise_fee_surcharge`, `generation_rates`, `export_generation_rate`.

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
franchise_fee_surcharge = 0.00042
pcia_rate = 0.03476

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
wrong total. Check that flag before acting on a price.

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
from nem_rates.tariff.eelec import EelecTariff

tariff = EelecTariff(Config.load())
for label, hour, kwh in [("off_peak", 12, 22.903), ("peak", 17, 0.458)]:
    rate = tariff.price_at(datetime(2026, 7, 29, hour, tzinfo=PACIFIC)).total
    print(f"{label}: {kwh} kWh @ ${rate:.5f} = ${kwh * rate:.2f}")
```

Compare the total against the sum of the per-kWh lines on both bills. They
should agree to within the bill's per-line rounding.
