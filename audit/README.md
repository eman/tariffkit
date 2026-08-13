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

`src/nem_rates` prices energy. This reads how one utility prints paper for one
account, and needs that account's login. A wheel carrying it would ship a
statement parser that fails on everybody else's bill. The build pins the
exclusion for both wheel and sdist.

The one thing that does belong in the library is the authenticated session and
Green Button download: a metered record fetched over HTTP is still a metered
record, which is what `nem_rates.sources` is for.

## Setup

```bash
cp audit/account.example.toml audit/account.toml   # gitignored
```

`account.toml` dates the *account* — schedule, supplier, baseline territory,
PCIA vintage — because those change over its life and `Config` describes one
moment. A cycle spanning a change is refused rather than guessed: no single
configuration priced it, and picking one produces a believable delta that gets
filed as a rounding mystery.

Statements are never committed. Point `NEM_RATES_STATEMENT_DIR` at wherever
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

Four cycles still carry findings. They are listed rather than tolerated,
because a harness that widens a tolerance until it agrees is worth nothing.

**MCE generation on the two 2025 summer cycles, about $0.36 each.** MCE
publishes only its current rate card and files no advice letters, so the
vintage that actually applied cannot be regenerated -- `regen cca --provider
mce --for-date` fails outright. `audit doctor` reports the card as 943 and 1125
days older than the cycles it prices, and the bill repeats it, so this arrives
as a named data gap rather than as an unexplained discrepancy. It is the larger
part of both cycles' difference.

**Distribution on those same two cycles, about $0.16 each: which hours the
energy arrived in.** Not the tariff, the parser, or the time-of-use rules --
each was ruled out by measurement rather than assumed.

The rate data is exact: the cells reconcile against the published totals, the
components sum to those totals to six decimals, and the statement prints
`@ $0.50269` and `@ $0.62569`, which are the vendored figures. The cycle's total
energy is right too, agreeing with the statement to 0.05 kWh. Every section of
the parse sums exactly.

What differs is the split. The statement bills 208.789 kWh at peak; the
InfluxDB series yields 201.892. Applying *our own* time-of-use rules to the
meter's own 15-minute registers, fetched as a Green Button export, gives
209.110 -- so the rules are right. At the roughly two-cent spread between peak
and off-peak distribution, the 6.9 kWh gap is the entire residual.

**Both series come from the same physical meter**, the Rainforest device simply
reading its register, so this is not two meters disagreeing. Nor is it a clock
offset: aligning the two hourly profiles is unambiguously best at zero shift
(31 kWh of absolute difference, against 200+ at one hour either way). What the
profile shows is the InfluxDB series running 1-3 kWh per hour below the
register through the evening -- hours 16 to 20 sum to exactly the -7.22 kWh the
source check reports -- while the cycle total stays right. Around 31 kWh is
reshuffled between hours and nets out.

That is an artefact of reconstructing intervals from cumulative counter
samples. Sampling is regular at five minutes, which cannot move energy across
an hour boundary on its own, so the remaining suspect is occasional gaps spread
pro rata across the hours they span. `_per_interval` already spreads rather
than crediting the later interval, which was worth 3.4 kWh of peak export when
it was fixed; this is the same class of error one level down.

`compare_sources` now compares the peak split as well as the total, because
totals cannot see it: on that cycle the two agree to 0.05 kWh on the cycle's
energy and disagree by 7.22 kWh about when it arrived, which is the only place
the money was.

**Two EV2-A cycles, +0.23 and +0.12** on Distribution + Public Purpose
Programs. Not a wrong distribution rate, which is the first thing to suspect and
the measurement rules out -- subtracting the Base Services Charge, which is
exact to the cent on every cycle, the residual per kilowatt-hour varies sixfold
across four cycles on the same schedule:

| cycle | days | kWh | residual | per kWh |
|---|---|---|---|---|
| 2026-03-10 | 32 | 730.6 | 0.184 | 0.00025 |
| 2026-04-07 | 29 | 41.1 | 0.020 | 0.00049 |
| 2026-05-06 | 29 | 376.2 | 0.006 | 0.00002 |
| 2026-06-05 | 32 | 71.6 | 0.109 | 0.00152 |

A rate error would hold the last column constant. It does not, and the two
cycles that miss are both 32 days while both 29-day cycles are within a
rounding of exact -- so the next place to look is how the utility apportions a
charge across a longer cycle, not the tariff.
