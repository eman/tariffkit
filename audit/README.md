# audit — reconciling computed bills against real statements

Every defect this library has shipped was found by checking a computed bill
against a real statement, by hand, once. That check found five: a CCA account
priced as bundled, a baseline credit frozen at the cycle's first day, vintage
tables inherited from the future, invented CCA credit, and a state tax modelled
nowhere. Nothing re-ran it. This does.

```bash
python -m audit parse ~/Desktop/PGE_20260205.pdf          # what the bill says
python -m audit reconcile ~/Desktop/PGE_*.pdf             # against what we compute
```

```
PGE_20260205.pdf  2025-12-30..2026-01-29 (31 days)  E-TOU-C / MCE
  billed $468.41   computed $468.42
  the utility split this cycle at a rate change: 2025-12-30..2025-12-31, 2026-01-01..2026-01-29
  0 mismatch, 0 unmapped line(s), 0 unmapped component(s), 14 matched
  RECONCILED
```

## Why it is not in the package

`src/tariffkit` prices energy. This reads how one utility prints paper for one
account, and needs that account's login. A wheel carrying it would ship a
statement parser that fails on everybody else's bill. The build pins the
exclusion for both wheel and sdist.

The one thing that does belong in the library is the authenticated session and
Green Button download: a metered record fetched over HTTP is still a metered
record, which is what `tariffkit.sources` is for.

## Setup

```bash
cp audit/account.example.toml audit/account.toml   # gitignored
```

`account.toml` dates the *account* — schedule, supplier, baseline territory,
PCIA vintage — because those change over its life and `Config` describes one
moment. A cycle spanning a change is refused rather than guessed: no single
configuration priced it, and picking one produces a believable delta that gets
filed as a rounding mystery.

Statements are never committed. Point `TARIFFKIT_STATEMENT_DIR` at wherever
yours already are, or let downloads land in `.cache/pge/statements/`. `*.pdf` is
gitignored.

Interval data comes from InfluxDB via `INFLUXDB3_*` in `.env`.

## Exit codes

| | |
|---|---|
| `0` | every statement reconciled |
| `1` | a mismatch, an unmapped line, or an unmapped component |
| `2` | the check could not be performed |

Two failure codes because "your numbers disagree" and "I could not check" call
for opposite responses, and one code makes a broken harness look like a billing
error.

## How it decides

A parse that does not add up is fatal before anything is compared. The statement
prints its utility total twice — as time-of-use lines split across any rate
change, and unbundled into components — and those two views share no rows, so
their agreement is the strongest evidence the parse is right.

`statements/mapping.py` maps printed lines to component keys. It is many-to-one,
because the utility combines components into one line, and occasionally
many-to-many, because it also splits one component across several lines at a
ratio it does not publish. Two invariants keep it honest and are enforced by
`check_map`: no component may be claimed twice, and any combination must say
why. `verified` records which statements a rule has actually reproduced, so the
report can separate what reconciles from what is assumed.

Three residuals are reported separately and never collapsed: an unmapped printed
line, an unmapped computed component — the sneakiest, since every line can agree
while the total is wrong — and a genuine mismatch.

## The portal

`pge/PORTAL.md` records what the account portal actually is: a Salesforce
Experience Cloud community with no REST endpoints, where even a bill PDF comes
back base64 inside an Aura JSON response. Read it before writing anything that
talks to it.

## Running it

```bash
uv run python -m audit run --since 2025-11-01 --until 2026-08-31
```

Lists every statement the portal holds for that range, downloads each, prices
the matching interval data, reconciles it line by line, and prints a table with
one row per cycle. Useful flags:

| flag | what it does |
|---|---|
| `--verbose` | show agreeing lines too, not just failures |
| `--json` | machine-readable output instead of the report |
| `--green-button` | also download PG&E's own interval export and compare the two meters |
| `--keep-statements` | leave the downloaded PDFs in `.cache/pge/statements/` |
| `--read-hour N` | move the cycle boundary off midnight |
| `--account PATH` | a different account history (default `audit/account.toml`) |

Statements are deleted after the run unless `--keep-statements`. One carries the
service address, the account number, and a remittance scanline with the account
embedded, so keeping a pile of them is not a side effect a billing check should
have without being asked.

Exit codes are load-bearing: `0` all reconciled, `1` a real disagreement, `2`
the check could not be performed.

## What does not reconcile yet

Over 2025-08 to 2026-08: **13 cycles, all priced, 9 reconciled clean**, and the
year's computed total is $0.88 from the billed one across $3,104. Statements
before November 2025 carry no text at all and are recovered by recognising the
rendered pages -- see `statements/ocr.py`, and `pge/PORTAL.md` for why.

**Every remaining difference has one cause: which hours the meter recorded.**
Not the rates, the vintages, the map, or the parser. The report says so on each
mismatching line, because `reconcile/attribution.py` re-prices the line from the
kilowatt-hours the statement itself printed, which removes the meter from the
question:

    Distribution + Public Purpose Programs     177.97    177.81    -0.16  MISMATCH
        priced from the statement's own kWh: 177.96 (-0.01) -- the rates
        reproduce this line, so the difference is which hours the meter recorded

Every mismatch in the year returns that verdict. The cycle totals agree with the
statement -- 0.05 kWh on a 701 kWh month -- while the time-of-use splits differ,
which is worth about two cents a kilowatt-hour where peak and off-peak
distribution diverge.

The cause is instrumented. Sampling is regular at five minutes, but the series
also carries outages -- on the 2025-10 cycle, four gaps over fifteen minutes
totalling 73 hours, the longest 71.8. Spreading those evenly gives the peak
window its share of the clock, five hours in twenty-four, rather than its share
of the load: 75.1 kWh reconstructed across 72 hours puts 15.6 kWh in peak where
the real shape puts about 22.4, a 6.8 kWh deficit against the 6.9 measured.
Readings reconstructed across a gap wider than an hour are marked `estimated`
and reported on the bill.

**The stale MCE rate card is not a cause, which the same check established.**
It is 940 to 1156 days older than the cycles it prices and the bill says so,
but pricing the generation line from the statement's own kilowatt-hours
reproduces it exactly -- 111.64 against 111.64. MCE evidently did not reprice in
between. This had been recorded here as the larger part of two cycles'
difference; it was not, and the check that decided it is now automatic rather
than a hand-run script.

Repairing the meter data would close the remaining $0.88, and is deliberately
not done: this tool exists to validate the bill calculator, and a gap in the
meter record is a sound reason for a computed bill to differ from a received
one so long as the reason is stated. Substituting better inputs until the
numbers agree would make the agreement worthless. (Measured, as a diagnostic
only: taking the meter's own 15-minute registers for the reconstructed hours
moves two cycles from -0.149 to +0.088 and -0.164 to +0.018.)
