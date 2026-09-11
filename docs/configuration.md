# Configuration

Every entry point (library, CLI, MQTT, web, Home Assistant) prices from the
same `Config` object. Get this right once and all of them agree.

## Where settings come from

`Config.load()` resolves in this order, later winning:

1. Built-in defaults (PG&E bundled, NBT26, PTO 2026-06-03).
2. `~/.config/tariffkit/config.toml`, or `$XDG_CONFIG_HOME/tariffkit/config.toml`.
3. An explicit `--config /path/to.toml`.
4. `TARIFFKIT_*` environment variables.

Check what actually resolved before trusting a number:

```bash
tariffkit info
```

The file contains tariff and endpoint settings, not credentials. It may reveal
your utility, rate plan, CCA, and PTO date, so keep it user-readable only:

```bash
chmod 600 ~/.config/tariffkit/config.toml
```

**This is the stateless path** — one `Config`, current settings only. If your
service agreement has ever changed tariff, supplier, or baseline territory,
and you want past bills to price with what was actually in force on their own
days, set up [your account](accounts.md) instead: every command prices from it
rather than from this file, and passing `--config` is how you opt back out for
one command. See [Pricing from the account](accounts.md#pricing-from-the-account)
for exactly how that choice is made.

### Utility identity

The account, configuration, and API payloads identify Pacific Gas and Electric
with the unambiguous machine value `pacific_gas_and_electric`. User interfaces
show `PG&E` or `Pacific Gas and Electric Company`. The distinct `pge` machine
value means Portland General Electric; TariffKit recognizes that identity but
does not yet ship Portland tariff data, so attempting to price it raises an
explicit unsupported-utility error.

## Credentials

Store long-lived credentials in the operating system's keyring rather than in
`config.toml`, shell history, command arguments, or a repository `.env` file:

```bash
pip install 'tariffkit[secrets]'
tariffkit credentials set pge.username
tariffkit credentials set pge.password
tariffkit credentials set home_assistant.token
tariffkit credentials set influxdb.token
tariffkit credentials set mqtt.username
tariffkit credentials set mqtt.password

tariffkit credentials list       # names only; values are never printed
tariffkit credentials delete mqtt.password
```

`set` prompts without echo, so the value never appears in process arguments or
shell history. macOS Keychain, Windows Credential Locker, and the configured
Linux Secret Service backend provide storage. Containers and unattended
services can continue to inject environment variables instead.

Secret precedence is: explicit library/CLI value, real environment variables,
`~/.config/tariffkit/.env`, then the OS keyring. Non-secret settings use:
defaults, `config.toml`, environment, then explicit arguments.
`tariffkit credentials list` prints where each one is actually resolving from.

### PG&E portal access

Portal credentials are needed for Green Button downloads, `account sync`
(see [Your account](accounts.md)), and the repository audit
harness; pricing itself remains offline regardless. Store them once:

```bash
tariffkit credentials set pge.username
tariffkit credentials set pge.password
```

Optional device-trust values use `pge.browser_cookie`,
`pge.validation_cookie`, and `pge.account_urn`. Non-secret account settings can
live in the main file:

```toml
[pge]
account_id = "service account identifier"
cookie_path = "~/.cache/tariffkit/pge/cookies.json"
```

The cookie cache is created with mode `0600`. `PGE_USERNAME`, `PGE_PASSWORD`,
`PGE_BROWSER_COOKIE`, `PGE_VALIDATION_COOKIE`, and `PGE_ACCOUNT_URN` remain
available for containers.

`tariffkit credentials list` says where each one is actually resolving from,
and never prints a value:

```console
$ tariffkit credentials list
keyring: keyring.backends.macOS.Keyring

  home_assistant.token   .env (HA_TOKEN)
  influxdb.token         .env (INFLUXDB3_AUTH_TOKEN)
  mqtt.password          not set
  mqtt.username          not set
  pge.account_urn        .env (PGE_ACCOUNT_URN)
  pge.browser_cookie     .env (PGE_BROWSER_COOKIE)
  pge.password           keyring
  pge.username           keyring
  pge.validation_cookie  .env (PGE_VALIDATION_COOKIE)

the environment and .env win over the keyring; values are never printed
```

The keyring line names the backend, so an all-`not set` listing tells you
whether nothing is stored or nothing can be read — a headless container with
no secret service reports `none available here`, and `TARIFFKIT_DISABLE_KEYRING=1`
does the same on purpose.

One account reads one set of credentials, so there is nothing to name or
select. See [Credentials](accounts.md#credentials).

## A worked example: PG&E delivery + MCE generation

```toml
# ~/.config/tariffkit/config.toml
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
| `tariff` | `E-1`, `E-ELEC`, `E-TOU-C`, `E-TOU-D`, `EV2-A` | Defaults to `E-ELEC` |
| `supplier` | `bundled`, `cca` | `cca` requires a `[cca]` table |
| `interconnection_year` | 2023–2026 | Picks the NBT vintage and ACC Plus rate |
| `pto_date` | ISO date | Starts the nine-year rate lock |
| `vintage` | `NBT23`…`NBT26`, `NBT00` | Overrides `interconnection_year` |
| `acc_plus_segment` | `residential`, `residential_low_income`, `none` | |
| `discount` | `none`, `care`, `fera` | Requires `acc_plus_segment = "residential_low_income"` |
| `base_services_charge_tier` | 1, 2, 3 | $/day, reported separately from $/kWh |
| `baseline_territory` | `P`…`Z` | E-1 and E-TOU-C only; your bill names it |
| `baseline_code` | `basic`, `all_electric` | PG&E's Code B / Code H |
| `medical_baseline` | Boolean | Adds the standard allowance on E-1/E-TOU-C or applies D-MEDICAL on E-ELEC/E-TOU-D/EV2-A |
| `medical_kwh_per_day` | Number or omitted | Overrides PG&E's generated 6,000 kWh/year standard medical quantity |
| `smartrate` | Boolean | Enables event-driven E-RSMART pricing |
| `smartrate_events` | ISO-date list | Authoritative SmartDay dates supplied by the caller |
| `smartrate_known_through` | ISO date | Last date on which absence from the event list means “not an event” |

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

All five currently active single-family residential schedules are vendored as
generated, effective-dated snapshots.

| Schedule | Periods | Baseline |
|---|---|---|
| `E-1` | no TOU; marginal price is Tier 2 | yes |
| `E-ELEC` | peak 4–9pm, part-peak 3–4pm and 9pm–12am, off-peak otherwise | no |
| `EV2-A` | same shape as E-ELEC, different rates | no |
| `E-TOU-C` | peak 4–9pm, **no part-peak** — off-peak otherwise | yes |
| `E-TOU-D` | peak 5–8pm on non-holiday weekdays; off-peak otherwise | no |

All share the June–September summer season. E-TOU-D alone distinguishes
non-holiday weekdays.

### Tiered and baseline credits

E-1 and E-TOU-C credit usage within a baseline allowance. That is a *quantity*,
not a time, so no marginal price can express it:

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

### Medical Baseline and SmartRate

`medical_baseline = true` follows the applicable published mechanism. E-1 and
E-TOU-C add the standard 6,000 kWh/year allowance from Rule 19; E-ELEC,
E-TOU-D, and EV2-A remove the Wildfire Fund Charge and apply D-MEDICAL's 12%
volumetric credit. The Base Services Charge is not discounted.

SmartRate events are not predictable tariff data. Set `smartrate = true`,
provide announced dates in `smartrate_events`, and set
`smartrate_known_through` to the last authoritative date. TariffKit applies the
generated E-RSMART high-price charge and cycle credits. Prices after the known
horizon carry `complete = false`; TariffKit never guesses an event from weather.
The event list may be empty when no events have been announced, but enabling
SmartRate without an authoritative horizon is rejected.

## Home Assistant

Only needed for `tariffkit bill --source ha`. Entity ids are configuration and
live in the config file; the access token is not and does not.

```toml
[home_assistant]
host = "https://homeassistant.example:8123"
import_entity = "sensor.grid_import_total"
export_entity = "sensor.grid_export_total"
```

Both entities are **optional and have no default** — entity names are
site-specific, and a guess is not a better starting point than no guess. Leave
them unset and rate pricing (`tariffkit now`, `forecast`, `info`) works
unchanged; only `tariffkit bill --source ha` needs them, and it says so by name
if they are missing.

When you do name them, prefer a **monotonic-filtered** entity. Meter readers
typically expose both a filtered counter and a raw device feed, and the raw one
drops to zero several times a day when the reader restarts its session with the
meter — differencing across that invents a huge interval and then a
compensating hole.

The access token can come from the OS keyring, `~/.config/tariffkit/.env`, or
the environment.
The file is not shell — spaces around `=` and quoted values are fine, and are
what the parser expects:

```ini
# ~/.config/tariffkit/.env
HA_HOST = "https://homeassistant.example:8123"
HA_TOKEN = "<long-lived access token>"
```

To use the environment instead, export them as shell variables:

```bash
export HA_HOST=https://homeassistant.example:8123
export HA_TOKEN=...
```

Resolution order, later winning: the config file, OS keyring, `.env`, real
environment variables, your account's source mapping, then
`--ha-import-entity` / `--ha-export-entity`. The account's mapping applies to a
bill, and does not change stateless/global behavior. It names grid import
(consumed from the grid, not whole-home load) and grid export separately.
`HA_TOKEN` is deliberately never read from the config file.

## Net Surplus Compensation

`nsc_rate` ($/kWh) is the rate used at the annual true-up. It is **unset by
default and should usually stay that way** until a cash-out statement exists:

```toml
nsc_rate = 0.031
```

MCE determines its Solar Billing Plan rate *at* cash-out and does not publish it
in advance, so there is no correct value to pre-fill. Left unset, the true-up
falls back to PG&E's published series (vendored, monthly) and marks the result
`estimated`. That series is PG&E's rate for **bundled** customers — a CCA account
is not eligible for PG&E NSC at all — so treat the fallback as an order-of-
magnitude stand-in, not a forecast. Also settable as `TARIFFKIT_NSC_RATE`.

## InfluxDB

Only needed for `tariffkit bill --source influx`. Same split as above: series
names are configuration, while the token comes from keyring or environment.

```toml
[influxdb]
host = "influxdb.example"
database = "homedb"
import_entity = "grid_import_total"
export_entity = "grid_export_total"
```

`host` may be a bare name (`https://` is assumed) or a full URL with a scheme
and port; `/api/v3/query_sql` is appended either way. As above the two series
are optional and have no default, and only `tariffkit bill --source influx`
needs them. Prefer the **raw** counters here, unlike the Home Assistant side —
they reach back much further, and the drop-to-zero artefacts are filtered out
on read. A `sensor.`
prefix is accepted and stripped, since InfluxDB stores the bare name. `table`
defaults to `sensor_numeric`, which is what Home Assistant's InfluxDB
integration writes.

```ini
# ~/.config/tariffkit/.env
INFLUXDB3_HOST = "influxdb.example"
INFLUXDB3_DATABASE = "homedb"
INFLUXDB3_AUTH_TOKEN = "<database token>"
```

Resolution order, later winning: the config file, keyring, `.env`, then real
environment variables (`TARIFFKIT_INFLUX_IMPORT_ENTITY` /
`TARIFFKIT_INFLUX_EXPORT_ENTITY` for the series), your account's
source mapping, then
`--influx-import-entity` / `--influx-export-entity`. As with `HA_TOKEN`,
`INFLUXDB3_AUTH_TOKEN` is never read from the config file.

Save the pair on your account with `tariffkit account source set influx ...
--apply`; use `ha` for Home Assistant. The source mapping is not
effective-dated because it identifies the data store, not tariff history.

## MQTT

MQTT settings also persist in the main config file, so publishing does not need
connection arguments on every invocation:

```toml
[mqtt]
broker = "mqtt.example"
port = 8883
topic_prefix = "tariffkit"
forecast_hours = 48
tls = true
```

Store `mqtt.username` and `mqtt.password` in the keyring. Explicit CLI
arguments remain available for ephemeral overrides.

## Environment variables

`TARIFFKIT_SUPPLIER`, `TARIFFKIT_VINTAGE`, `TARIFFKIT_INTERCONNECTION_YEAR`,
`TARIFFKIT_PTO_DATE`, `TARIFFKIT_ACC_PLUS_SEGMENT`, `TARIFFKIT_DISCOUNT`,
`TARIFFKIT_BSC_TIER`, `TARIFFKIT_BASELINE_TERRITORY`,
`TARIFFKIT_BASELINE_CODE`, and `TARIFFKIT_NSC_RATE`.

Containers can provide a complete CCA object as `TARIFFKIT_CCA_JSON`; for
example:

```bash
export TARIFFKIT_SUPPLIER=cca
export TARIFFKIT_CCA_JSON='{"name":"MCE","rate_card":"mce","pcia_vintage":2011}'
```

## CCA service

Under NEM 3.0 a CCA customer receives only the **delivery** component of the
export credit from PG&E. Generation compensation comes from the CCA, and the
two halves of your import price come from two different bills.

The ACC Plus adder is the exception to that split: where a provider credits one
of its own — MCE does — you are paid it **twice**, once by each party at the
same $/kWh, and `export_price.total` carries both. They do not behave alike.
PG&E's is spent against delivery charges in the cycle that earns it, so its
Bonus Credits balance normally closes at $0.00; MCE's banks as the Energy
Export Bonus Credit (EEBC), is never applied while export credit remains to
spend, and settles on MCE's Solar Billing Plan year rather than PG&E's True-Up.

One rate card is vendored:

| Provider | `rate_card` | Covers |
|---|---|---|
| MCE (Marin Clean Energy) | `"mce"` | ELEC generation by season/period, Cost Relief Credit (expires 2026-12-31), Deep Green premium, 10% Solar Bonus Credit, its own ACC Plus adder banked as the EEBC |

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

## Billing cycle

`tariffkit bill` with no `--start`/`--end` prices the cycle open right now. It
takes the boundary from the portal's own list of billed cycles where credentials
are stored, or from imported statements; where there is neither, this is the
meter-read day it falls back to:

```toml
[billing]
cycle_start_day = 29    # 1-31; clamps in short months
```

Unset, the fallback is the calendar month, which will not match a bill and says
so when it is used. See
[The default window](billing.md#the-default-window).

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
from tariffkit import Config, PACIFIC
from tariffkit.tariff.retail import RetailTariff

tariff = RetailTariff(Config.load())
for label, hour, kwh in [("off_peak", 12, 22.903), ("peak", 17, 0.458)]:
    rate = tariff.price_at(datetime(2026, 7, 29, hour, tzinfo=PACIFIC)).total
    print(f"{label}: {kwh} kWh @ ${rate:.5f} = ${kwh * rate:.2f}")
```

Compare the total against the sum of the per-kWh lines on both bills. They
should agree to within the bill's per-line rounding.
