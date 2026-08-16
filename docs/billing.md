# Bill calculator

Prices a billing cycle from interval meter data. Pure and dependency-free:
readings in, decomposed charges out. It does not know or care where the readings
came from.

```bash
tariffkit bill intervals.csv --start 2026-07-02 --end 2026-07-28
tariffkit bill - --json < intervals.csv
```

## Where readings come from

Three sources, all in `tariffkit.sources`. Green Button is the default and needs
nothing installed beyond the core package:

```bash
tariffkit bill pge_electric_usage_interval_data_....csv --start 2026-07-02 --end 2026-07-28
tariffkit bill - --json < intervals.csv
```

`--source csv` is still accepted as a spelling of `--source green-button`, but
"CSV" says nothing about *which* CSV, so the documented name is the format.

Home Assistant reads the meter directly, so there is no download step:

```bash
pip install 'tariffkit[ha]'
tariffkit bill --source ha --start 2026-07-29 --end 2026-08-09
```

For a named account, configure its source once and omit entity flags:

```bash
tariffkit account source home set ha \
  --grid-import-entity sensor.grid_import \
  --grid-export-entity sensor.grid_export --apply
tariffkit bill --account home --source ha --start 2026-07-29 --end 2026-08-09
```

Grid import means energy consumed from the grid, not whole-home load. Grid
export is energy returned to the grid. A one-off
`--ha-import-entity`/`--ha-export-entity` (or Influx equivalents) flag takes
precedence over the profile, which takes precedence over environment and
global configuration; otherwise the source default is used.

It pulls **long-term statistics**, not state history. That is the only place a
whole cycle survives — the history behind `/api/history` is purged on the
recorder's schedule, typically ten days, while statistics are kept indefinitely.
Statistics are WebSocket-only, which is why this needs the `ha` extra;
`tariffkit.billing` itself stays stdlib-only.

Home Assistant keeps two resolutions for different lengths of time, so the
default asks for both and prefers the finer one wherever it exists:

| Period | Kept for | Used |
|---|---|---|
| `5minute` | about the recorder's window | recent cycles |
| `hour` | indefinitely | everything older |

One run can therefore mix them, and the CLI says which it used rather than
implying uniformity:

```
  source: Home Assistant statistics (2541 x 5minute, 76 x hour)
```

Force one with `--ha-resolution 5minute|hour`.

### Two things to expect from this source

**"Both import and export" warnings are normal here.** Import and export are
metered separately, so a slot carrying both is real rather than un-netted gross
data, and the coarser the slot the more often it happens — on one real week, 42%
of active hours against 12% of five-minute slots. That is why the finer
resolution is preferred where it exists.

**Statistics restart their running sum when recording is interrupted**, and the
first point after the break reports the entire accumulated total as that
period's change. One real instance put 543.663 kWh inside a five-minute slot,
about 6,500 kW against a service that tops out near 48. Anything implying more
than 100 kW is discarded with a warning naming the timestamp, and the hole it
leaves is reported by the usual coverage check rather than filled in.

## InfluxDB

If Home Assistant also writes sensor samples to InfluxDB 3, the raw meter
counters are there as a plain time series, and reading them directly is more
accurate than either of the other two sources:

```bash
pip install 'tariffkit[influx]'
tariffkit bill --source influx --start 2026-06-30 --end 2026-07-28
```

Set an InfluxDB pair on a named profile with
`tariffkit account source home set influx --grid-import-entity NAME
--grid-export-entity NAME --apply`, then use
`tariffkit bill --account home --source influx ...`. These names identify the
grid-import and grid-export counters; they are not whole-home consumption
entities.

Energy over a window is a cumulative counter's endpoints, so the total does not
depend on how densely it was sampled in between. Against the July 2026
statement, all three sources on the same cycle:

| Source | Imported | Exported | MCE credit | Delivery credit |
|---|---|---|---|---|
| Green Button | 39.060 | 193.320 | 9.59 | 6.22 |
| Home Assistant | 39.902 | 193.795 | 9.68 | 6.73 |
| InfluxDB | 39.902 | **193.793** | **9.64** | **6.30** |
| *billed* | *39.906* | *193.797* | *9.63* | *6.25* |

Green Button is low because PG&E rounds every interval to two decimals before
exporting it, which costs about 2% of a low-import month. Its compensating
strength is timing: it is the utility's own record at true fifteen-minute
metering, which is why its credit split was the best of the three until the
InfluxDB source started spreading advances pro rata.

The gap between the two live sources is subtler and worth understanding, because
it is the one thing a counter series can get wrong. **A sample reports an advance
since the previous sample, not an instant.** Crediting the whole advance to the
interval holding the later sample pushes energy forward across every boundary it
spans, and boundaries are where the money is — the export delivery credit is
roughly 500× larger during the 4–9pm peak than outside it. On that cycle the
naive rule put 55.52 kWh of export in peak where Green Button's 15-minute data has
52.08. This source spreads each advance pro rata over the span it actually
covers, giving 52.62. Home Assistant's statistics are pre-aggregated by Home
Assistant using the forward-crediting rule, which is why its credit components
sit further from the statement despite identical totals.

Sampling density still bounds how fine an interval is meaningful. On this data
the median gap is five minutes but the 90th percentile is three quarters of an
hour, so hourly is the default; `--influx-resolution 15` is interpolation, not
measurement, and should be checked against the density for the period.

Two smaller notes:

- Defaults are the **unfiltered** counters — the opposite of the Home Assistant
  source's defaults, and deliberate. They reach back fourteen months against the
  filtered pair's five, and the drop-to-zero behaviour that makes them unusable
  raw is repaired here anyway (about one sample in ten).
- Entity ids are constrained to `[A-Za-z0-9_.]` rather than escaped, because they
  are interpolated into SQL. A `sensor.` prefix is stripped; InfluxDB stores the
  bare name.

## Green Button input

Green Button is the industry format for handing a customer their own meter data;
PG&E exposes it as **"Download my data"** on the usage page, which returns a file
named like `pge_electric_usage_interval_data_<account>_<dates>.csv`, generally at
fifteen-minute resolution.

**This reads the CSV form, not the XML one.** Green Button also has an ESPI/XML
serialisation; that is a different parser and is not implemented.

It is Green Button first rather than Green Button only. The preamble skipping and
the default column names exist for PG&E's export, but every column is
configurable, so an inverter log or a statistics dump works too. Columns are
auto-detected from the header; common names for each field are recognised
(`start`/`timestamp`/`datetime`, `imported`/`delivered`/`usage`,
`exported`/`received`/`production`, `net`).

```csv
start,imported,exported
2026-07-02T00:00:00-07:00,0.35,0
2026-07-02T09:00:00-07:00,0,2.6
```

A signed `net` column works too, with positive meaning import. Interval length is
inferred from the closest pair of timestamps, so 15-minute and hourly data both
work without configuration.

Timestamps without a UTC offset are assumed Pacific, since that is what the
tariff is anchored to. Supplying offsets is better: a naive timestamp on the
autumn DST transition is ambiguous without one.

Override detection when needed:

```python
from tariffkit.sources import GreenButtonLayout, read_green_button

readings = read_green_button(
    "meter.csv",
    GreenButtonLayout(
        start="Interval Start",
        imported="Consumption (kWh)",
        exported="Surplus (kWh)",
    ),
)
```

## Library use

```python
from datetime import date
from tariffkit import Config, RateEngine
from tariffkit.billing import BillEngine, BillingPeriod
from tariffkit.sources import read_green_button

engine = BillEngine(RateEngine(Config.load()))
bill = engine.compute(
    read_green_button("intervals.csv"),
    BillingPeriod(date(2026, 7, 2), date(2026, 7, 28)),
)

bill.total  # charges + credits + fixed
bill.energy_charges  # positive
bill.export_credits  # negative
bill.fixed_charges  # Base Services Charge over the cycle's days
bill.buckets  # per season/TOU period, mirroring printed bill lines
bill.import_components  # {'distribution': 43.27, 'cca_generation': 42.74, ...}
```

Omit the period and it is inferred from the readings' own span. Readings outside
the period are ignored, so a year of data can be billed one cycle at a time
without slicing it first.

## Named account profiles

`--account NAME` prices against a [named account profile](accounts.md)
instead of a single `Config`:

```bash
tariffkit bill intervals.csv --start 2026-07-02 --end 2026-07-28 --account home
```

For a cycle that stays within one epoch, this produces exactly the figures a
plain `tariffkit bill` with that epoch's settings would. Its purpose is the
cycle that does not: when the profile records a tariff, supplier, or
baseline-territory change effective partway through `[start, end]`,
`--account` tiles the cycle into one `Segment` per epoch active during it
(via `AccountProfile.segments_for()`) and prices each stretch under its own
snapshot, the same way PG&E's own statement prints separate blocks for a
mid-cycle change rather than blending the two rates. The output shape does
not change — one `Bill` for the whole period — only how it was computed.

`--account` and `--config` are mutually exclusive here as everywhere else;
see [Selecting a profile](accounts.md#selecting-a-profile).

## Reading the output

`buckets` mirror how a statement prints: one line per season and TOU period,
with an effective `$/kWh`:

```
              imported        $     exported        $
off_peak        85.050    31.70      421.200   -26.33
part_peak       43.200    18.55       70.200    -4.35
peak            97.200    57.47       70.200    -8.32
```

`import_components` and `export_components` decompose those totals by rate
component, which is what makes a computed bill checkable against a real one line
by line. Charges are positive and credits negative, so everything sums directly
into `total`.

`effective_import_rate` is the blended rate actually paid across the cycle. It
is **not** a marginal rate; do not dispatch on it. Use
`RateEngine.price_at()` for that.

## Data quality

A bill computed over a lossy series is silently wrong: it just looks like a
month with less usage. So coverage is checked rather than assumed, and problems
appear in `bill.warnings`:

- gaps and overlaps in the series
- readings covering materially less than the period
- intervals reporting **both** import and export, which suggests gross data that
  was never netted

Pass `check=False` to skip, or `--no-check` on the CLI.

## Two independent signals

`warnings` and `complete` answer different questions, and neither implies the
other:

| | Question | Goes bad when |
|---|---|---|
| `bill.warnings` | Is the meter data sound? | gaps, overlaps, short coverage, un-netted intervals |
| `bill.complete` | Are the rates fully known? | a priced hour was incomplete or inexact, e.g. an unconfigured CCA generation rate card |

A bill can reconcile against a real statement to a fraction of a percent and
still carry coverage warnings; it can cover the period perfectly and still be
priced from rates that are estimates. Check both before trusting a total.

```python
if bill.warnings:
    for warning in bill.warnings:
        log.warning("%s", warning)
if not bill.complete:
    log.warning("priced from incomplete or inexact rates")
```

## Netting

Under the Net Billing Tariff, import and export net **within an interval**, and
the finer the interval the less self-consumption offsets. Real AMI data arrives
already netted by the meter, so it is used as-is; this deliberately does not
re-net to a coarser or finer granularity.

For gross data from an inverter or CT clamps, net it explicitly:

```python
IntervalReading.from_gross(start, consumption_kwh=1.0, production_kwh=4.0)
# -> exported 3.0
```

`hourly()` collapses sub-hourly readings for grouping while summing each
direction separately, so it never changes what the bill totals.

## Carrying credits between cycles

`BillEngine` prices one cycle. Credits earned but not spent bank and offset later
charges, which is stateful, so it lives in a ledger on top:

```python
from tariffkit.billing import CreditBalances, apply_credits, run_ledger

entry = apply_credits(bill, CreditBalances(generation=4.93))
entry.applied.total  # spent this cycle
entry.closing.total  # carried forward
entry.cash_due  # what is actually owed

run_ledger(bills, opening)  # fold a run of cycles, carrying the bank
```

Credits are **not fungible**, and the statement states the rule: Energy Produced
credits offset only Energy Produced charges, Energy Delivered credits only
Energy Delivered charges, and the bonus credit offsets anything not
non-bypassable. So a balance is three buckets, and scoped buckets are spent
before the bonus — otherwise the flexible credit is burnt on charges a scoped one
could have covered, stranding the scoped credit.

Two things to know:

- **One bank at a time.** A CCA customer has two, kept separately by PG&E and by
  the CCA. A `Bill` merges both providers, so applying the ledger straight to one
  is an approximation. Feed one provider's charges and credits for an exact
  answer.
- **The charge scoping is only partly reconciled**, which `LedgerEntry.complete`
  reports as `False`. Which components bank into which bucket is confirmed, and
  so is unspent credit carrying forward. Where the boundary of an "Energy
  Delivered charge" falls is not: confirming it needs a cycle whose credits
  exceed the charges they may offset, so the cap binds.

## The annual true-up

Closing a year is a separate module, because for a CCA account it is two events
on two calendars that do not line up:

```python
from tariffkit.billing import run_ledger, run_true_ups

ledger = run_ledger(bills)
run_true_ups(ledger.entries, pto_date=config.pto_date, is_cca=True)
```

|  | MCE Annual Cash-Out | PG&E Relevant Period |
|---|---|---|
| Ends | after the **March–April** billing cycle | on the **PTO anniversary** |
| Fixed for | every customer alike | this account alone |
| Covers | generation credits | delivery credits, ACC Plus |
| Pays surplus | yes, at MCE's NSC rate | **no** — a CCA account is barred |

An account with a June PTO date therefore cashes out with MCE in April and
trues up with PG&E in June, and neither closes the other's bank.

**PG&E pays a CCA account no Net Surplus Compensation at all.** Schedule NBT,
Special Condition 5.a: net surplus generators receiving "Community Choice
Aggregation (CCA) Service from a CCA are not eligible to receive NSC from PG&E".
Applicability is limited to "all bundled Net Surplus Generators". PG&E publishes
an NSC rate monthly and it is vendored here, but for a CCA account it is a
stand-in, not the rate that gets paid.

**Credits do not expire**, which is worth saying because the opposite is widely
repeated. Schedule NBT carries excess credits "forward to the customer's next
Relevant Period", forfeited only on leaving the tariff; MCE's SBP tariff rolls
the balance over "indefinitely". The annual reset to zero belongs to NEM 2.0.

Surplus is a **kilowatt-hour** test in both tariffs — exported energy exceeding
imported energy over the period — not a dollar balance. When it is met, the
export credit already paid for that energy is reversed at the average export
credit rate (including MCE's Solar Bonus Credit) so the same kilowatt-hours are
not paid for twice, and the reversal is charged against the balance first and
the payment second.

### What is not settled

`TrueUp.verified` is always `False`. Nothing here has been checked against a
statement, because none exists yet: the first MCE cash-out falls after the
March–April 2027 cycle and the first PG&E Relevant Period ends 2027-06-03.
`tariffkit.billing.trueup.OPEN_QUESTIONS` records the two places the tariff text
supports more than one reading — whether the credit reversal and the NSC rate
are two steps or one, and whether MCE's $5,000 cap and "NSC + $0.02/kWh" formula
(both published under its NEM 1.0/2.0 program) carry over to the SBP.

`Config.nsc_rate` is unset by default and the result is marked `estimated`,
because MCE determines its SBP rate *at* cash-out rather than publishing it in
advance. The fallback also degrades to the latest published month when the
true-up month is still in the future, which it will be for the first cash-out.

## What this does not do

Two known limits:

- A **rate change mid-cycle** is not prorated. The Base Services Charge is priced
  from the tariff in force at the start of the cycle; PG&E prorates. Cycles
  spanning a rate change (or the June 1 / October 1 season boundary, for the
  fixed charge specifically) will be slightly off.
- **PCIA basis is assumed.** Your bill prints PCIA as a dollar amount with no
  rate or kWh, so its `$/kWh` is derived. If the real basis is gross rather than
  netted import, computed bills drift from actual by that difference.

## Verifying against a real statement

The highest-value check is reconciling against a bill you already have. Sum the
per-kWh lines across both the PG&E and CCA statements and compare to
`bill.energy_charges`; compare the Base Services Charge line to
`bill.fixed_components`.

`tests/test_billing.py` does exactly this against a July 2026 MCE/PG&E
statement, and is the template for adding your own.
