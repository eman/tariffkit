# Bill calculator

Prices a billing cycle from interval meter data. Pure and dependency-free:
readings in, decomposed charges out. It does not know or care where the readings
came from.

```bash
nem-rates bill intervals.csv --start 2026-07-02 --end 2026-07-28
nem-rates bill - --json < intervals.csv
```

## CSV input

Columns are auto-detected from the header; common names for each field are
recognised (`start`/`timestamp`/`datetime`, `imported`/`delivered`/`usage`,
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
from nem_rates.billing import CsvLayout, read_csv

readings = read_csv(
    "meter.csv",
    CsvLayout(
        start="Interval Start",
        imported="Consumption (kWh)",
        exported="Surplus (kWh)",
    ),
)
```

## Library use

```python
from datetime import date
from nem_rates import Config, RateEngine
from nem_rates.billing import BillEngine, BillingPeriod, read_csv

engine = BillEngine(RateEngine(Config.load()))
bill = engine.compute(
    read_csv("intervals.csv"),
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
from nem_rates.billing import CreditBalances, apply_credits, run_ledger

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

## What this does not do

It does **not** model the annual true-up, Net Surplus Compensation, or the credit
reversal at cash-out. Those need a published NSC rate and the expiry rules for
unspent credit, neither vendored, and neither checkable against a statement until
a true-up cycle exists. MCE's program year runs April to March.

Two more known limits:

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
