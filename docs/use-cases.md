# Use cases

Four questions people bring to a bill calculator. They differ in one thing that
decides everything else: **how far back the calculation has to remember.**

| | Question | Memory | Resets |
|---|---|---|---|
| [1](#1-a-house-without-solar) | What do I owe this cycle? | one cycle | every meter read |
| [2](#2-a-house-with-solar) | What do I owe, with a credit bank? | PTO → true-up | annually, at true-up |
| [3](#3-realized-payback) | What has solar actually saved me? | PTO → now | never, within the system's life |
| [4](#4-would-another-rate-plan-be-cheaper) | Would another rate plan be cheaper? | one historical cycle | n/a — counterfactual |

Everything below is runnable. Each section is written to be lifted into a sample
CLI app: the recipe is the program, and the traps are the reason it is not
three lines shorter.

## The one thing to get right first

`Bill.total` is **not what you owe.** It subtracts every export credit from
every charge, and the tariff does not allow that. Export credits are scoped —
generation credits reach only generation charges, delivery credits only delivery
charges — so credit beyond what its own bucket can absorb banks instead of
reducing the bill.

```python
bill.total  # charges minus ALL credits -- can go negative
apply_credits(bill).cash_due  # what a statement would actually charge
```

On a house without solar the two agree, and `Bill.total` is fine. On any
exporting account they diverge sharply, and use cases 2, 3 and 4 are all wrong
if you use the first one. See [Carrying credits between cycles](billing.md#carrying-credits-between-cycles)
for why the buckets exist.

## 1. A house without solar

No Permission To Operate, so no export compensation, no credit bank, and no
annual true-up. The calculation has no memory longer than the billing period: it
opens at the meter read date and closes at the next one.

**The one thing that is not simple** is that the rate plan can change mid-cycle.
The utility does not blend the rates — it prints two blocks on one statement and
prices each by its own schedule. An [account profile](accounts.md) records the
change as an epoch, and `segments_for` turns a cycle into the blocks that cycle
was actually governed by.

```python
from tariffkit.billing import BillingPeriod
from tariffkit.billing.engine import compute_segments
from tariffkit.cli import AccountStore

profile = AccountStore().load()
period = BillingPeriod(date(2026, 7, 29), date(2026, 8, 28))

bill = compute_segments(profile.segments_for(period), readings)
bill.total  # here it IS what you owe: nothing was exported
bill.energy_charges  # the per-kWh lines
bill.fixed_charges  # the Base Services Charge
```

`compute_segments` handles the single-schedule case too — one segment in, one
bill out — so a CLI app never needs to branch on whether anything changed.

**Trap.** A rate change mid-cycle is not prorated for the *fixed* charge; it is
priced from the schedule in force at the cycle's start, while PG&E prorates. The
same applies at the June 1 and October 1 season boundaries. See
[What this does not do](billing.md#what-this-does-not-do).

## 2. A house with solar

Permission To Operate starts the clock. From PTO onward exports earn credit, and
credit not spent in the cycle that earned it **banks** and carries forward. The
bank is closed once a year at true-up, which is the only thing that resets it.

So the memory runs PTO → first true-up → second true-up → and so on. Nothing
before PTO is inside it, and nothing before the last true-up needs to be
re-priced to answer "what do I owe now".

```python
from tariffkit.billing import run_ledger, run_true_ups

bills = [compute_segments(profile.segments_for(p), readings) for p in cycles]
ledger = run_ledger(bills)  # folds the bank cycle to cycle

entry = ledger.entries[-1]
entry.cash_due  # what this cycle owes
entry.applied.total  # credit that reached charges
entry.closing.total  # credit carried forward
```

A true-up is a separate step because a CCA account has two, on calendars that do
not line up — MCE cashes out after the March–April cycle, PG&E on the PTO
anniversary:

```python
run_true_ups(ledger.entries, pto_date=config.pto_date, is_cca=True)
```

**Traps, in the order they bite.**

- **Two banks, not one.** A CCA customer's generation credits sit on the CCA's
  page and delivery credits on the utility's. `CreditBalances.held_by` splits
  them; adding them gives a number no statement prints and that never settles as
  a whole.
- **A bank you cannot vouch for should not be spent.** `BankState.trustworthy`
  is false whenever the fold carries a warning, and the amount owed is then
  stated *before* any bank offsets it. That is the safe direction, but a caller
  has to say so — see `TariffKitData.opening_note` in the Home Assistant
  component for the shape.
- **Pre-PTO exports are not an error.** Energy left the house before the
  arrangement existed; the meter saw it and the tariff grants nothing for it.
  That is reported as `Bill.uncompensated_kwh`, a figure, not a warning — it
  says what happened, not that anything is wrong.

## 3. Realized payback

How much of the investment has been earned back through reduced grid cost. This
reaches back across cycles — but never earlier than PTO, because there is no
solar arrangement before it.

The method is a counterfactual: price the **house load** as if all of it had come
from the grid, and compare with what was actually billed.

```python
actual = compute_segments(profile.segments_for(period), metered_readings)
without = compute_segments(profile.segments_for(period), load_as_import)

saved = apply_credits(without).cash_due - apply_credits(actual).cash_due
```

`load_as_import` is one reading per hour with `imported=<house load>,
exported=0.0`. One cycle of a real account:

```
  house load                     777.5 kWh
  grid import (actual)            28.3 kWh
  grid export (actual)           331.5 kWh
  self-supplied                  749.2 kWh

  bill without solar         $  356.37
  bill as billed             $   23.04
  realized saving this cycle $  333.34
  credit banked, not yet realized $108.17
```

**Traps.**

- **Measure the load; do not infer it hour by hour.** Total consumption is
  `import - export + production`, which is right over a cycle and wrong within
  it once a battery shifts load between hours — and a time-of-use tariff prices
  the hours, not the total. Use a measured house-load counter where one exists
  (`sensor.sigen_plant_total_load_consumption` on the account above); reconstruct
  only if you must, and say that you did.
- **Banked credit is not realized.** It is earned and not yet spent. Report it
  beside the saving rather than inside it, or a heavy-export summer reads as a
  bigger return than the customer has actually received. `entry.closing.total` is
  that figure.
- **Sum `cash_due`, not `Bill.total`,** for the same reason as everywhere else.
- **The counterfactual keeps the fixed charge.** A house without solar still pays
  the Base Services Charge every day, so it appears on both sides and cancels.
  Dropping it from one side overstates the saving.

## 4. Would another rate plan be cheaper?

Take a historical cycle with real metered data — Green Button, AMI, or recorder
statistics — and price it again under a different schedule. Same readings, same
period, one field changed:

```python
from dataclasses import replace

for tariff in ("E-ELEC", "EV2-A", "E-TOU-C"):
    config = replace(base_config, tariff=tariff)
    bill = BillEngine(RateEngine(config)).compute(readings, period)
    print(tariff, apply_credits(bill).cash_due)
```

The same real cycle, ranked both ways:

| tariff | energy | fixed | earned | applied | banked | **due** | `Bill.total` |
|---|---|---|---|---|---|---|---|
| E-ELEC | 10.83 | 24.60 | 120.57 | 9.02 | 108.17 | **23.04** | −85.14 |
| EV2-A | 8.00 | 24.60 | 120.57 | 6.72 | 111.00 | **23.04** | −87.96 |
| E-TOU-C | 10.23 | 24.60 | 120.57 | 11.20 | 106.47 | **20.74** | −85.73 |

`Bill.total` ranks EV2-A cheapest by $2.82. What you actually owe is *identical*
on E-ELEC and EV2-A, and E-TOU-C is the cheapest of the three. Both corrections
matter:

- **E-ELEC vs EV2-A.** Their whole difference is $2.30 of delivery charges, and
  this account has more delivery export credit than that — so the credit absorbs
  the difference exactly and the two plans are indistinguishable on the bill.
  Credit that would have banked instead gets spent; nothing reaches the customer.
- **E-TOU-C wins on something else entirely** — its baseline credit, which shows
  up as $2.29 of negative non-offsettable charge, below the floor any export
  credit can reach.

**Traps.**

- **The fixed charge may not differentiate plans.** Since 2026-03-01 the Base
  Services Charge is per-meter and income-tiered under AB 205, identical across
  E-ELEC, EV2-A and E-TOU-C — which is why it is $24.60 in all three rows.
  Comparing cycles *before* that date is a different matter: E-TOU-C and EV2-A
  had no daily fixed charge at all and E-ELEC had a flat one.
- **A pre-PTO cycle is a legitimate thing to price** — a counterfactual can reach
  back before the solar arrangement. Exports in it earn nothing, and
  `Bill.uncompensated_kwh` says how much energy that was, so a comparison can
  report it instead of silently showing no export credit.
- **Check `bill.complete` before trusting a comparison.** It is false when any
  priced hour used incomplete or inexact rates, which is a statement about the
  rate data rather than about the meter — and a plan you are not licensed to
  price accurately should not win a comparison.

## Where the readings come from

All four use cases take the same input: a list of `IntervalReading`. See
[Where readings come from](billing.md#where-readings-come-from) for Green Button
CSV, Home Assistant statistics, and InfluxDB, and
[Netting](billing.md#netting) if your data is gross inverter output rather than
metered net.
