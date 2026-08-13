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
