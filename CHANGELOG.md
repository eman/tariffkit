# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **`nem_rates.regen`**, shipped inside the package and reachable as
  `nem-rates regen`, rebuilds every vendored dataset from what publishers
  publish. It replaces the two scripts under `tools/`, which were sdist-only: a
  released wheel that carries rates its user cannot refresh is only useful until
  the next advice letter.
- Four datasets, each with a check that has to pass before anything is written.
  `tariff` reads a utility's retail sheets and proves the unbundled components
  sum to the sheet's own published totals, then hands the rendered file to a real
  `RetailTariff` and compares the price it computes. `accplus` reads the export
  tariff's adder table and reads it back through the library. `cca` reads a CCA's
  generation rate card. `export` collapses the hourly export archive, verified
  cell by cell.
- **CCA rate cards are no longer hand-transcribed.** A CCA supplies generation
  only, so its card is one rate per schedule, season and period. Every MCE value
  currently equals PG&E's generation component exactly, but the card is still
  extracted and stored separately and regeneration *reports* the parity rather
  than requiring it — deriving one from the other would be less code and would
  silently produce wrong prices the day MCE moves. Schedules a card lists but the
  library does not vendor are skipped and named.
- **Publishers are declared, not hard-coded.** `nem_rates.regen.providers` holds
  every publisher-specific fact; PG&E is not the only utility and MCE is not the
  only CCA, so adding either is a registry entry rather than a parser change.
  A source also records whether a script can fetch it, because publishers differ
  arbitrarily: MCE's CDN answers urllib and curl with 403 whatever headers they
  send, and answers httpx with 200 and the file, so the fetcher tries httpx
  first. A source that genuinely cannot be fetched is skipped with a note rather
  than failed — unknown, not stale — and regenerates from a file supplied with
  `--pdf`.
- A document with no text layer is diagnosed rather than reported as an empty
  table. MCE's current rate card is a print-to-PDF export whose font maps six
  characters to Unicode: every figure is a glyph id with no character behind it,
  so no parser can recover it and only OCR could. Their 2023 card extracts
  exactly, which is what the CCA extractor is tested against, so `cca/mce.toml`
  stays hand-maintained until the publisher ships readable text again.
- A new `regen` extra carries `pypdf`. The library itself never opens a PDF.
- The weekly job now checks all four datasets rather than export rates alone,
  and reports each independently so the first failure does not hide the second.

### Fixed
- **The three tariff snapshots are now dated from the sheet that carries the
  rates**, not the latest date in the tariff book. A tariff book reissues pages
  independently: on all three schedules the totals page is Advice 7921-E
  effective 2026-06-01 while the unbundled rate table is 7846-E effective
  2026-03-01, and the two reconcile exactly, so those values have been in force
  since March. Hand transcription had reached both answers from the same
  evidence — E-ELEC and E-TOU-C dated June, EV2-A March. Every rate value is
  unchanged; what changes is that April and May 2026 now price instead of
  raising "no snapshot effective on or before".

### Added
- **`nem-rates bill --source influx`** reads the raw meter counters from
  InfluxDB 3 over `/api/v3/query_sql`. A cumulative counter's total depends only
  on its endpoints, so this is exact regardless of sampling density: against the
  July 2026 statement it reproduced 39.902 kWh imported against 39.906 billed and
  193.793 exported against 193.797, where PG&E's own CSV export loses about 2% of
  a low-import month to two-decimal rounding. Configuration is a `[influxdb]`
  section, credentials `.env` or the environment; needs the new `influx` extra.
- The InfluxDB source spreads each counter advance **pro rata over the span it
  accrued across** rather than crediting it to the interval holding the later
  sample. A sample reports an advance since the previous sample, not an instant,
  and forward-crediting biases energy across every boundary it spans — expensive
  when the export delivery credit is roughly 500x larger during the 4-9pm peak
  than outside it. On one real cycle the naive rule put 55.52 kWh of export in
  peak where PG&E's 15-minute data has 52.08; spreading gives 52.62, and moves
  the modelled delivery credit from $6.73 to $6.30 against $6.25 billed.
- The InfluxDB source defaults to the **unfiltered** counters, opposite to the
  Home Assistant source and deliberately: they reach back fourteen months against
  the filtered pair's five, and the drop-to-zero artefacts that make them
  unusable raw — about one sample in ten, emitted while the Eagle-100
  re-establishes its meter session — are filtered on read.
- **`nem-rates bill --source ha`** reads interval data straight from Home
  Assistant, so a cycle can be billed without downloading anything. It pulls
  long-term statistics rather than state history, which is the only place a whole
  cycle survives — history is purged on the recorder's schedule, typically ten
  days. Entity ids come from a `[home_assistant]` config section (defaulting to
  the Rainforest Eagle-100 pair) and credentials from `.env` or the environment;
  the token is never read from the config file. Needs the new `ha` extra, since
  statistics are WebSocket-only; `nem_rates.billing` stays stdlib-only and the
  source lives in `nem_rates.sources`.
- The source asks for both statistics resolutions and prefers five-minute
  wherever it still exists, falling back to hourly. That is not just tidiness:
  import and export are metered separately, so a slot carrying both directions
  is real rather than un-netted gross data, and it happened in 42% of active
  hours against 12% of five-minute slots on one real week.
- Statistics points implying more than 100 kW are discarded with a warning. When
  recording is interrupted the running sum restarts and the first point after the
  break reports the whole accumulated total as its change — 543.663 kWh inside
  one five-minute slot on a real instance. Discarding leaves a hole the coverage
  check reports, rather than a plausible-looking invention.
- **Export credit ledger** (`nem_rates.billing.ledger`), the stateful layer above
  the pure per-cycle engine: credits earned but not spent bank and offset later
  charges. `apply_credits` handles one cycle, `run_ledger` folds a run.
  A balance is three buckets rather than a number, because credits are not
  fungible — the statement's own rule is that Energy Produced credits offset only
  Energy Produced charges, Energy Delivered only Energy Delivered, and the bonus
  credit anything not non-bypassable. Scoped buckets are spent before the bonus,
  so the flexible credit is not burnt on charges a scoped one could cover.
  Reconciled against both credit banks on the 2026-08-04 statement: PG&E's spends
  everything it earns ($7.96 in, $7.96 out), and MCE's earns more than it can
  spend ($4.93 + $11.33 − $3.63 = $12.63), which exercises the cap in both
  directions. `LedgerEntry.complete` reports `False` while the charge scoping is
  only partly reconciled.
- **Two more rate schedules: E-TOU-C** (Time-of-Use, peak 4–9 p.m. every day)
  and **EV2-A** (Home Charging). Both transcribed from their June 2026 tariff
  sheets, with all ten new rate cells verified against the published totals.
  Select with `tariff = "E-TOU-C"` / `"EV2-A"`.
- **E-TOU-C baseline credit.** The bill prints one credit; the sheet implements
  it as two Conservation Incentive Adjustment rates whose spread is $0.08140.
  It applies to a *quantity* rather than a time, so `price_at` returns the
  over-baseline price — correct for dispatch — and reports the credit as
  `ImportPrice.baseline_credit`. The billing engine, which sees a whole cycle,
  applies it as a `baseline_credit` line, accumulating the allowance day by day
  so a cycle crossing the season boundary is right. Needs `baseline_territory`
  and `baseline_code` in config; without a territory there is no credit line,
  since the quantities vary several-fold and guessing would be worse.
- The complete PCIA vintage table, 2009 through 2026, from Schedule E-ELEC
  Sheet 5. Eleven vintages were missing (2010 and 2012–2020), so
  `pcia_vintage = 2011` — the vintage named on a real MCE statement — raised
  `ConfigError` and the only way through was a hand-derived `pcia_rate`.
- The Schedule E-FFS franchise fee surcharge table, 2009 through 2026,
  residential. It is vintaged off the same year as the PCIA, so **setting
  `pcia_vintage` now resolves both** and a CCA price reaches `complete = True`
  without any hand-entered rates. The tariff data previously recorded that this
  surcharge was "not published and must not be guessed"; it is published, in a
  separate schedule.
- `read_csv` reads PG&E's interval export as downloaded. It previously failed on
  three things at once: the account preamble before the header row, a timestamp
  split across `DATE` and `START TIME` rather than one ISO column, and
  unit-suffixed names like `IMPORT (kWh)`. Header matching now folds case and
  punctuation, `CsvLayout` gained `date`/`time` for split pairs, and the preamble
  is located by scanning for the header rather than assuming a fixed offset.
- `nem_rates.interop`: adapters publishing rates in formats existing energy
  management systems already read. `resample()` splits the hourly curve onto
  shorter slots, `predbat.payload()` builds Predbat's `raw_today` /
  `raw_tomorrow` lists, and `emhass.forecast_payload()` builds EMHASS's
  `load_cost_forecast` / `prod_price_forecast`.
- Both the custom component and the MQTT publisher expose those payloads as
  attributes on the import and export price sensors, so Predbat can be pointed
  at them via `metric_octopus_import` / `metric_octopus_export` and EMHASS
  runtime parameters need no reshaping.
- Documented Energy dashboard setup. The existing `USD/kWh` price sensors
  already satisfied Home Assistant's price-entity validation, for both grid
  consumption and return-to-grid compensation; only the documentation was
  missing.
- `TOU Period` now declares `device_class: enum` with its three options.

### Fixed
- `BillingPeriod.elapsed` reports the cycle's real span, and coverage now
  compares against it. `days` times 24 hours is an hour out either way on a cycle
  containing a DST transition, and on the autumn one it errs toward hiding a
  short series -- the same direction as the gap bug below. `days` still counts
  calendar days, which is what the Base Services Charge is billed on.
- Coverage checking measured real time as clock time, so both DST transitions
  were wrong and in opposite directions. On the autumn day an hour missing from
  the data was hidden: 01:45 plus fifteen minutes reads as 02:00 while the clock
  has meanwhile gone back, so `find_gaps` saw contiguity across a real one-hour
  hole. On the spring day a contiguous series looked broken, because the labels
  skip an hour that never existed. Both matter against real data — PG&E's own
  export emits 96 intervals for the 25-hour autumn day, omitting the repeated
  hour entirely, which is exactly the silently-short bill this check exists to
  catch.
- Naive timestamps on the autumn DST transition are now disambiguated on ingest.
  01:00 occurs twice and `zoneinfo` resolves both to `fold=0`, so an hour of
  readings priced as PG&E's HS1 instead of HS2 and coverage reported the file as
  overlapping itself. Meter exports are chronological, so a row whose instant
  does not advance, and which advances once `fold=1` is applied, belongs to the
  second pass. Rows carrying an explicit offset are unaffected.
- `PricePoint.end` was computed by wall-clock arithmetic, so on the autumn DST
  transition the first 01:00 claimed a two-hour span overlapping the second.
  Now stepped in absolute time. Consumers reading the explicit start/end pairs
  need them contiguous and disjoint.
- `timeutil.next_hour` had the same wall-clock bug, contradicting its own
  docstring. The MQTT publisher sleeps until this, so on the autumn transition
  it slept two hours and never published the second 01:00 — leaving retained
  topics an hour stale.
- `forecast --format table` rendered the autumn transition as
  `01:00 PDT - 01:00`, a seemingly zero-length hour, because only the start
  carried `%Z`.

### Changed
- The EV2-A snapshot is dated from the 2026-03-01 Base Services Charge
  restructure rather than from its advice letter. The rates come from a sheet
  carrying Advice 7921-E effective 2026-06-01, but a 2026-04-01..04-29 statement
  bills them exactly, so dating the snapshot from the letter made that whole
  cycle unpriceable. Back-dating cannot misapply the summer rates the letter did
  set, since the summer season starts June 1.
- **CCA rate cards are keyed by PG&E schedule.** A CCA prices each schedule
  separately and the rates differ substantially — MCE winter off-peak is 0.06754
  on E-ELEC against 0.11042 on E-TOU-C — so the card previously applied E-ELEC's
  rates to any schedule, understating an E-TOU-C customer's generation by nearly
  half. `CcaRateCard.generation` now takes the schedule and raises for one the
  card does not cover, rather than falling back. `mce.toml` vendors E-ELEC,
  E-TOU-C and EV2-A, each verified at parity with PG&E's generation component.
- `EelecTariff` is now `RetailTariff`, in `nem_rates.tariff.retail`. It was
  already schedule-agnostic — everything that varies lives in the vendored
  snapshot — so the name had stopped being true. Not part of the top-level
  public API.
- A schedule whose sheet does not publish a CARE or FERA percentage now raises
  when one is requested, rather than falling back to another schedule's figure.
  Neither the E-TOU-C nor the EV2-A sheet prints them.
- `[periods].part_peak` is optional. E-TOU-C has no part-peak at all.
- `Bill.complete` is now purely a statement about the rates, as its own docstring
  always claimed. Coverage problems travel in `Bill.warnings` alone rather than
  also clearing `complete`. Conflating them meant a bill that reconciles against
  a real statement to 0.2% still described itself as an estimate, because the
  meter data contained a few intervals reporting in both directions. Callers
  wanting "trust this total" should check both, and `docs/billing.md` now spells
  out which question each answers.
- MCE exports are no longer flagged as an estimate. `export_credit_basis` is now
  `acc_generation` with `export_credit_verified = true`, so an MCE export price
  reports `complete = True`. MCE still does not publish its credit matrix, but
  pricing 2,784 quarter-hourly meter intervals against the matching statement put
  every export component within 0.3%: generation credit −0.1%, delivery −0.2%,
  ACC Plus −0.3%, solar bonus +0.2% — inside the rounding of the bill's own
  displayed dollars. One summer cycle on one account, so the seasonal spread is
  untested; the claim it supports is structural.
- Large derived attributes (forecast, EMHASS series, Predbat lists, component
  breakdown) are excluded from Home Assistant's recorder. The coordinator
  refreshes every minute; recording a 48-hour curve 1,440 times a day was never
  useful history.
- Predbat rate lists are published in cents rather than dollars, because Predbat
  assumes pence per kWh and its thresholds are tuned to that magnitude.
- EMHASS series are bare positional lists at 30-minute resolution, matching the
  `optimization_time_step` its `config_defaults.json` ships, and are trimmed to
  the slot EMHASS is currently in. `prediction_horizon` is published alongside
  so it cannot drift out of step with the list length.

### Notes
- **EV2-A is reconciled against a real statement**, the first for any schedule
  other than E-ELEC and the first winter cycle on any of them. All three delivery
  rates, all three MCE generation rates, the cost relief credit, the Base
  Services Charge, and every flat rider on the statement's own breakdown page
  reproduce exactly; energy charges, MCE net charges and the fixed charge all
  land within a cent. `tests/test_ev2a.py` pins it.
- The vendored E-ELEC and E-TOU-C **winter** rates still have no statement behind
  them. They first apply in October 2026.
- Both tables were reconciled against two statements for a 2011-vintage MCE
  account: $0.03492 × 23.589 kWh = $0.82 and × 39.906 kWh = $1.39;
  $0.00060 × 23.589 = $0.01 and × 39.906 = $0.02, all four as billed. With them,
  a full CCA import price reproduces that account's bill exactly rather than to
  the $0.00015/kWh that hand-derived rates achieved.

## [0.1.0] - 2026-07-28

Initial release.

### Added
- `RateEngine` with `price_now()`, `price_at()`, and `forecast()` for PG&E
  E-ELEC import prices and NEM 3.0 / Net Billing Tariff export credits.
- Vendored rate data for all five NBT vintages (NBT23/24/25/26/00), collapsed
  from PG&E's ~40 MB-per-vintage hourly files to 268 KiB total with verified
  lossless round-tripping.
- ACC Plus adder as a first-class, separately reported component.
- CCA / Direct Access support: bundled generation and PCIA are dropped, and
  delivery-only prices are flagged `complete=False` rather than understated.
- CLI: `now`, `forecast`, `info`, `mqtt`, `serve`.
- MQTT publisher with Home Assistant MQTT Discovery and a last-will
  availability topic.
- FastAPI service under the `web` extra.
- HACS-installable Home Assistant custom component.
- `tools/regen_data.py` for refreshing vendored data, plus a weekly CI job that
  fails when upstream rates change.

### Notes on upstream data
- PG&E's export files label the repeated 01:00 on the autumn DST transition as
  `HS2`, so that hour is priced as 2am. Handled in `timeutil.export_hour`.
- From 2036 onward PG&E's own hour labels stop tracking Pacific daylight time
  and NBT25/26/00 duplicate some holidays onto the following day. The verified
  boundary is recorded per vintage as `exact_through` and surfaced as
  `ExportPrice.exact`. Every year within a nine-year rate lock is exact.
- Holiday calendars are extracted per vintage from the source data rather than
  recomputed, because the vintage files disagree in far-future years.
