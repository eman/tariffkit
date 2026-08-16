# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- **Static typing now covers the Home Assistant custom component, and Ruff
  enforces function annotations across the repository.** This closes the gap
  where core library and audit code passed strict mypy while integration code
  could drift from Home Assistant's typed service, entity, and config-entry
  contracts.
- **The Home Assistant custom component's config flow was redesigned around
  account history, and account identity no longer depends on mutable
  values.** Both setup and options used to be one flat form listing every
  field unconditionally -- CCA fields shown even for a bundled customer,
  export fields shown even without one -- with no concept of a profile's
  dated history at all. Setup is now a staged wizard -- account and tariff,
  then delivery and export, then a CCA product step only when your supplier
  is one -- that asks only the fields your earlier answers make relevant, and
  a new [**Account history**](docs/home-assistant.md#account-history) options
  submenu (inspect, add, edit, remove, import, export) brings the CLI's named
  account profiles (`docs/accounts.md`) into the integration for the first
  time, alongside a separate **Forecast and Predbat** options page. A config
  entry's identity is now the profile's stable local name: the old unique ID was
  `f"{tariff}-{supplier}-{interconnection_year}-{pto_date}"`, which changed
  out from under an entry the moment you updated your rate plan or PTO date
  and could collide with an unrelated account that happened to share all four
  values. New entries use `profile:<name>`, so tariff/history edits do not
  change identity and importing the same named account twice is rejected.
  The entry's title now reflects your profile name (or `supplier:tariff`)
  instead of a hardcoded "PG&E Rates" for every account.
- **Custom component entities got leaner.** The coordinator now hands sensors
  a typed `TariffKitData` (a forecast point, a rolling forecast, aggregate
  quality, trimmed provenance) instead of an ad hoc dict, and large or
  fast-changing attributes -- the forecast's `rates` list, and Predbat's
  `raw_today` / `raw_tomorrow` when enabled -- are excluded from the recorder.
  The separate Base Services Charge sensor is removed: it is a $/day fixed
  charge, not a $/kWh marginal price, and mixing it into the same device as
  the Energy dashboard's price entities produced nonsense there; read it via
  `engine.daily_fixed_charge()` instead ([docs/library.md](docs/library.md)).
  A new **Rate Forecast Through** timestamp entity replaces the forecast that
  used to ride along on every price sensor's attributes. `manifest.json`'s
  `iot_class` is now `calculated`, matching what the integration has always
  actually done -- computing from local static data, with no network call to
  ever go offline.
- **EMHASS forecasting moved from sensor attributes to an action**,
  `tariffkit.get_emhass_forecast`, and **Predbat compatibility is opt-in**
  instead of always computed. Both used to ride along on the import/export
  price entities' attributes on every coordinator refresh whether or not
  anything read them; EMHASS's shape needs a caller-chosen window rather than
  whatever the coordinator's own forecast horizon happens to cover, and most
  installs do not run Predbat at all. See
  [docs/home-assistant.md](docs/home-assistant.md#emhass) and
  [docs/home-assistant.md](docs/home-assistant.md#predbat).

### Added
- **A Docker Compose Home Assistant development environment** now bind mounts
  the custom component, local TariffKit source, and an isolated HA
  configuration directory. This makes integration changes testable against a
  real Home Assistant container without copying files or publishing the Python
  package; the accompanying container guide also defines the proposed
  production boundaries for the API and MQTT publisher.
- **Two response-returning actions**, `tariffkit.get_rates` and
  `tariffkit.get_emhass_forecast`, registered once per Home Assistant
  instance rather than per config entry, so they stay callable --
  including from **Developer Tools → Actions** -- even before an entry has
  finished loading. Both take an explicit window (`start`/`end`, `date`, or
  `horizon`, at a chosen `resolution`) capped at 168 hours, reject an
  ambiguous DST-fold timestamp or a window that does not align to the
  resolution with a named `ServiceValidationError` rather than guessing, and
  return the same quality and provenance data as the entities. See
  [docs/home-assistant.md](docs/home-assistant.md#actions).
- **Diagnostics support.** **Settings → Devices & Services → TariffKit → ⋮ →
  Download diagnostics** returns a sanitized snapshot -- schema version,
  whether the entry is loaded, forecast and Predbat settings, aggregate
  quality, trimmed provenance, and the cached forecast's span -- that
  deliberately omits the account's full profile, its observations, and any
  meter-source mapping.
- **[docs/home-assistant-quality.md](docs/home-assistant-quality.md)**, a
  dedicated rule-by-rule self-assessment against every published Bronze
  through Platinum rule in the Home Assistant Integration Quality Scale, with
  a justification for each exemption (no device or service to discover,
  poll, or reauthenticate to) and measured, reproducible test-coverage
  numbers rather than a claim -- distinct from a short summary and pointer
  left on the main integration page.
- **A genuine Home Assistant custom-component test harness**
  (`tests/test_ha_component.py`, with `pytest-homeassistant-custom-component`
  as a test dependency) covering the config and options flows, legacy entry
  migration, entity behaviour, both actions, diagnostics, Energy dashboard
  compatibility, DST handling, and opt-in Predbat -- the integration had no
  automated coverage of its own before this.

### Notes
- `CONFIG_VERSION` is now `3`. An entry created before this schema exists is
  migrated automatically on load, preserving its prices and every entity's
  existing unique ID and history; a migration TariffKit cannot make sense of
  fails the entry with a logged reason rather than guessing. See
  [docs/home-assistant.md](docs/home-assistant.md#account-history).

### Added
- **Profile-scoped meter sources.** Named accounts can store provider-neutral
  grid-import (consumed-from-grid) and grid-export entity pairs for Home
  Assistant and InfluxDB 3. `tariffkit bill --account NAME --source ha|influx`
  uses the saved pair, with explicit CLI entity flags taking precedence over
  the profile, then environment/global settings, then source defaults. The
  `tariffkit account source NAME show|set` command previews by default and
  persists only with `--apply`; mappings are not effective-dated.
- **Named account profiles.** `tariffkit account
  init/list/show/history/update/import-statement/sync/export` track a service
  agreement's history as an ordered set of dated `Config` snapshots
  (`AccountProfile`), so a bill for a cycle that spans a tariff, supplier, or
  baseline-territory change prices each stretch under the settings that were
  actually in force on its own days rather than today's. `--account NAME`
  selects a profile on `now`, `forecast`, `info`, `bill`, `mqtt`, and `serve`;
  `tariffkit bill --account` tiles a cycle into per-epoch segments
  automatically. Profiles are stored under
  `$XDG_CONFIG_HOME/tariffkit/accounts/<name>.json` with atomic, fsync'd
  writes and optimistic-concurrency conflict detection, so an interrupted or
  racing write cannot corrupt or silently overwrite one. See
  [docs/accounts.md](docs/accounts.md).
- **A public local PG&E statement importer**, moved out of the repository-only
  audit harness into `tariffkit.providers.pge` under the new
  `tariffkit[statements]` extra. `account import-statement` and `account sync`
  parse a local or portal-downloaded statement PDF (with an OCR fallback for
  bills with no text layer), reconcile its printed facts against a profile's
  history, and report each change as `ADD`, `CONFIRM`, `CONFLICT`, or
  `MISSING_REQUIRED` -- a change set with any conflict or missing value cannot
  be applied, so a profile is always either fully caught up to a statement or
  untouched by it. Only facts a statement actually prints are ever proposed;
  nothing is inferred past what it shows.
- **Named credential sets.** `tariffkit credentials set NAME --set SET_NAME`
  and `account init/update --credential-set SET_NAME` let more than one
  account profile share one PG&E portal login without storing the password
  twice.
- **REST and Home Assistant profile support.** `POST` pricing endpoints accept
  a `profile`/`account` selector alongside the existing `config` key, and
  `/v1/meta` reports `account_profile`/`account_effective` when one is active;
  an unknown or unreadable name returns a uniform, non-disclosing 404. The
  Home Assistant options flow gained a menu (current settings, account
  history, add/edit/remove a transition, import/export a profile) over the
  same credential-free profile storage, and migrates older config entries
  that predate it.
- `tariffkit account init --audit-file PATH` explicitly migrates an existing
  legacy audit account file, so adopting profiles does not require re-entering
  a service agreement's history by hand and a public command never probes
  repository-local developer configuration implicitly.

### Changed
- **The statement importer and reconciler are no longer audit-only.** The
  packaging ADR ([docs/packaging_strategy.md](docs/packaging_strategy.md)) is
  revised: reading and reconciling a PG&E statement was always generic,
  dependency-light logic with no maintainer-specific content, and now ships
  publicly as part of the distribution. What remains repository-only in
  `audit/` is narrower than "statement parsing" -- only the line-to-component
  mapping, attribution rules, run orchestration, and portal-protocol research
  genuinely specific to reconciling this project's own real statements
  against computed bills.
- **The project is now TariffKit.** The distribution, import package, CLI,
  configuration directory, environment prefix, MQTT default, repository links,
  and Home Assistant domain use the globally neutral `tariffkit` identity. This
  is an intentional clean break before the first public release; the initial
  provider scope remains PG&E and is documented as such.
- **Packaging remains one public distribution in one repository.** Optional
  runtime dependencies stay behind extras, while repository-only rate-data
  generation moved to `tools.regen` and is excluded from wheel and sdist.
- **Home Assistant now installs an exact TariffKit release requirement** instead
  of vendoring the complete source tree. This prevents maintainer code and local
  caches from leaking into HACS artifacts.
- **Development dependencies now use PEP 735 dependency groups**, package
  licensing uses an SPDX expression, versions are read from installed metadata,
  and release automation uses PyPI Trusted Publishing with attestations.
- **Python 3.14 is now the minimum.** Support for 3.11, 3.12 and 3.13 is
  dropped, and the CI matrix that spanned them is replaced by a single 3.14 job
  -- it had never tested the version this is developed on, so a regression could
  only be caught on versions nobody runs.

  For Home Assistant users this raises the floor to **2026.3.0**, the first
  release built on Python 3.14; `hacs.json` is updated to match. On an earlier
  Home Assistant the integration installs and then fails setup, because pip
  cannot satisfy the requirement.

### Added
- **Persistent private configuration across every active surface.** The CLI
  continues to load tariff, PTO, CCA, source, and MQTT settings from the XDG
  config file, while `tariffkit credentials` stores long-lived source and MQTT
  credentials in the operating-system keyring without putting values in argv or
  printing them. Environment injection remains available for containers.
- **Request-scoped REST pricing configuration.** POST variants of meta, current,
  point-in-time, and forecast endpoints accept a validated `Config` object for
  one request and reject unknown keys, including credentials.
- **Complete Home Assistant tariff setup.** Config entries now cover the rate
  plan, baseline, vendored or custom CCA generation, PCIA overrides, PTO, and
  forecast settings. The integration does not collect credentials because its
  runtime is local and credential-free.
- **`nem-rates regen tax`** and the California Energy Resources (Electrical
  Energy) Surcharge, read from CDTFA's numbered notices. A state tax rather than
  a utility tariff -- imposed whoever supplies the generation -- and billed on
  the generation provider's page, which on a CCA account is the CCA's. That is
  how it went unmodelled while every line on the utility's pages reconciled to
  the cent, and it was the whole of the remaining 29c on a real statement.
  CDTFA issues a notice only when the rate changes, so the notices are exactly
  the vintages that exist: L-971 for 2025, L-1020 for 2026.
- `--for-date` widens its search backwards on its own instead of asking for a
  `--scan` range nobody can guess. Filings are numbered sequentially across
  everything a utility files, so how far back a date sits is something the index
  discovers rather than something the caller knows.
- `Bill.taxes`, kept out of `Bill.energy_charges` because a statement prints
  taxes on their own line and does not total them with the energy lines -- the
  July 2026 statement's six energy lines come to $8.90 with its Energy
  Commission Tax separate -- but included in `Bill.total`, because they are
  owed.

The December 2025 / January 2026 statement now reconciles to **0.003%**:
$468.42 modelled against $468.41 billed.

### Added
- **Historical rate vintages, reconciled against a real statement.** PG&E tariff
  snapshots now run 2025-01-01, 2025-03-01, 2025-09-01, 2026-01-01 and
  2026-03-01, and MCE's rate card has a pre-repricing vintage, so cycles back to
  early 2025 price with the rates that were actually in force.
  `nem-rates regen tariff --for-date` finds the filing that adopted a vintage by
  indexing the utility's advice letters, rather than needing its number.
- Tables published on their own schedule are resolved by date too: the vintaged
  PCIA from the filing that last restated it, the franchise fee surcharge from
  the E-FFS version in force. Both are republished only when they change, so
  reading the current one for a historical snapshot silently applied today's
  values to an old cycle.

### Fixed
- Named account-profile updates now lock each profile across the revision check
  and filesystem mutation, preventing simultaneous writers or deletes from
  silently replacing a revision they did not read. Profile storage also leaves
  the caller-owned XDG root's permissions unchanged.
- Effective-dated provenance now resolves at the priced timestamp in the core
  engine and Home Assistant actions. Action responses describe contiguous
  provenance segments when a requested window crosses a tariff or account
  epoch instead of labeling the whole result with the coordinator's current
  configuration.
- Statement-derived account updates reject gaps and overlaps between printed
  service-agreement spans, Home Assistant profile imports cannot change a
  config entry's stable profile identity, and MQTT environment profile
  selection correctly overrides legacy aliases in the config file.
- **The baseline credit is applied per day at each day's own rate.** It had been
  read once at the cycle start, which put December's rate on all 300.70 kWh of a
  cycle the statement splits at 19.40 kWh @ $0.10084 and 281.30 @ $0.09566.
- Export compensation starts at Permission To Operate; before it, exports earn
  nothing whatever the meter recorded, and the bill says how much was
  uncompensated.
- `Config(supplier="cca")` now coerces to the enum. `Supplier` is a `StrEnum`,
  so the string compared equal but failed the identity tests every branch uses,
  and a CCA customer was priced as bundled -- silently, with plausible numbers.
- Vintage data is carried forward only from earlier vintages, never later ones.
  Backfilling had inherited the *current* snapshot's tables, giving a 2025 cycle
  2026's PCIA and MCE's 2023 card a cost relief credit that did not exist until
  2026.

The December 2025 / January 2026 statement now reconciles on all fourteen of its
lines to within five cents, and on the total to 0.06%.

### Added
- **`nem-rates regen tariff --advice-letter NUMBER`** rebuilds a superseded rate
  vintage from the filing that adopted it. The tariff book only ever serves what
  is current, so this is the only way to recover history — and without it the
  library could not price January or February 2026 at all. All three PG&E
  schedules are backfilled to 2026-01-01 (Advice 7797-E), each reconciling
  against that filing's own published totals.
- Vintages differ in shape, not only in value, and the extractor now handles the
  differences rather than assuming today's layout: the base services charge is
  absent on most schedules before AB 205 began it on 2026-03-01 and flat rather
  than tiered on E-ELEC, PCIA rows are spelled `2009 Vintage` in a filing and
  `2009` in the book, and a sheet's own header identifies which schedule it
  belongs to so one filing can be narrowed to one schedule.
- `daily_fixed_charge` returns zero for a vintage that had no such charge, which
  is the right answer rather than a missing-data error.

### Fixed
- The PCIA vintage table is read with its own row pattern rather than one that
  needs the figures at end of line. The last row runs into the next page's
  header — `2026 Vintage ($0.01011) (N) (L)U 39Oakland, California` — so a
  trailing match dropped the newest vintage, which is the worst one to lose. It
  had been masked: extraction previously failed outright and the values were
  silently carried forward from the prior snapshot.

### Added
- **A CCA card that cannot be parsed is still watched.** The vendored file records
  a checksum of the document its values were read from, so the weekly check
  downloads the card and reports whether the publisher has moved. Detection never
  needed a text layer, only bytes, and detection was always the more valuable
  half of a scheduled check — extraction being manual does not mean staleness
  has to be invisible. A change is reported rather than failed, since it needs a
  person and a permanently red job gets ignored; a card with no recorded checksum
  *is* a failure, because that is the one state where silence and safety look
  the same.

### Fixed
- `cca/mce.toml`'s 16 generation rates, its cost relief credit and its Deep Green
  premium were all re-read from MCE's published card and confirmed. The header
  previously said thirteen of them had never been checked against anything MCE
  published; they have now, and it says so. The card has no text layer, so this
  is a read of the rendered page rather than a parse -- which `docs/data.md` now
  documents as the procedure for that case rather than describing it as a
  blocker. Reading a rendered page needs a *reader* rather than a parser, not a
  person: an agent session does it directly. What a CI runner lacks is the
  reader, not the capability, which is why it detects the change by checksum and
  leaves the reading to a session that can.

### Added
- **`nem-rates regen nsc`** rebuilds the Net Surplus Compensation series from the
  published rate table. It was vendored by hand when the annual true-up landed
  and had no regeneration path at all, which made it the one dataset that could
  go stale silently — it grows by a row a month, and nothing about a missing
  month looks wrong until a true-up falls in it.
- **The franchise fee surcharge is read from Schedule E-FFS** rather than carried
  forward from the previous snapshot. It sits in a tariff snapshot's `[cca]`
  table but is published in a different schedule, so carrying it meant PG&E could
  reissue E-FFS with nothing noticing — and it is live rate data on every CCA
  price. `regen tariff` now reads both documents and reports which. All eighteen
  extracted vintages match the hand-transcribed ones exactly.

### Changed
- **The CSV reader moved to `nem_rates.sources.greenbutton` and is named for the
  format it reads.** It sat in `nem_rates.billing.ingest` while the other two
  sources lived in `nem_rates.sources`, on the reasoning that it had no optional
  dependency to isolate. That was the wrong organising principle: what a module
  reads is more useful than what it happens to import, and the one file-based
  source was the hardest to find. `read_csv` is now `read_green_button` and
  `CsvLayout` is `GreenButtonLayout`, both exported from `nem_rates.sources`
  rather than `nem_rates.billing`.
- `--source csv` is now `--source green-button`. "CSV" named a container rather
  than a format and could have meant any of several exports; Green Button is the
  industry standard PG&E publishes under "Download my data", and naming it also
  makes explicit that this reads the **CSV** form and not the ESPI/XML one. The
  old spelling still works.
- The Green Button parsing tests moved to `tests/test_sources_greenbutton.py`,
  alongside the tests for the other two sources.

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
  so no parser can reach them. The page still renders, so the table can be read
  from it and the values entered by hand, which is what the message now says --
  the distinction is between "no automated extraction" and "no data", and only
  the first is true. Their 2023 card extracts exactly, which is what the CCA
  extractor is tested against.

- **Annual true-up** (`nem_rates.billing.trueup`), the layer that closes a year
  on the credit bank. For a CCA account this is two events on two calendars that
  do not line up: MCE's Annual Cash-Out follows the March-April billing cycle and
  is the same for every customer, while PG&E's Relevant Period ends on the
  account's own PTO anniversary. An account with a June PTO date cashes out with
  MCE in April and trues up with PG&E in June, and neither closes the other's
  bank; modelling one annual event would be wrong for at least one of them.
- **PG&E pays a CCA account no Net Surplus Compensation.** Schedule NBT, Special
  Condition 5.a bars net surplus generators taking CCA service, and applicability
  is limited to "all bundled Net Surplus Generators". PG&E's published NSC series
  is vendored as `data/nsc/pge.toml` because it is the only auditable one, but
  for a CCA account it is a stand-in and results computed from it are flagged
  `estimated`.
- **Excess credits carry forward rather than expiring.** Schedule NBT carries
  them "forward to the customer's next Relevant Period", forfeited only on
  leaving the tariff; MCE's SBP tariff rolls the balance over "indefinitely". The
  annual reset to zero that is widely described belongs to NEM 2.0.
- Surplus is tested in kilowatt-hours, not dollars, per both tariffs. When a
  customer is a Net Surplus Generator the export credit already paid for that
  energy is reversed at the average export credit rate including MCE's Solar
  Bonus Credit, charged against the balance first and the payment second.
- `Config.nsc_rate` and `NEM_RATES_NSC_RATE`, unset by default because MCE
  determines its Solar Billing Plan rate at cash-out rather than publishing it in
  advance. `LedgerEntry` gained `imported_kwh` / `exported_kwh` to carry the
  surplus test.
- Nothing in the true-up is reconciled against a statement, so `TrueUp.verified`
  is always `False`; the first MCE cash-out falls after the March-April 2027
  cycle. `trueup.OPEN_QUESTIONS` records the two places the tariff text supports
  more than one reading.
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
- `read_green_button` reads PG&E's Green Button export as downloaded. It
  previously failed on three things at once: the account preamble before the
  header row, a timestamp split across `DATE` and `START TIME` rather than one
  ISO column, and unit-suffixed names like `IMPORT (kWh)`. Header matching now
  folds case and punctuation, `GreenButtonLayout` gained `date`/`time` for split
  pairs, and the preamble is located by scanning for the header rather than
  assuming a fixed offset. (Both were added under their former names,
  `read_csv` and `CsvLayout`, earlier in this same unreleased cycle; they are
  written here as they ship.)
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

- `cca/mce.toml` now records **how** its values were obtained and which are
  independently confirmed. They were read visually from the rendered rate card
  on 2026-08-01 rather than parsed, so a transcription slip has no automatic
  check behind it the way a PG&E sheet does. Three of the sixteen -- summer
  E-ELEC peak, part-peak and off-peak -- reconcile against the July 2026
  statement; the other thirteen, including every winter rate, do not, and winter
  first applies in October 2026. The previous header cited the source URL
  without distinguishing the two.
- A new `regen` extra carries `pypdf`. The library itself never opens a PDF.
- The weekly job now checks all four datasets rather than export rates alone,
  and reports each independently so the first failure does not hide the second.
- **The three tariff snapshots are now dated from the sheet that carries the
  rates**, not the latest date in the tariff book. A tariff book reissues pages
  independently: on all three schedules the totals page is Advice 7921-E
  effective 2026-06-01 while the unbundled rate table is 7846-E effective
  2026-03-01, and the two reconcile exactly, so those values have been in force
  since March. Hand transcription had reached both answers from the same
  evidence — E-ELEC and E-TOU-C dated June, EV2-A March. Every rate value is
  unchanged; what changes is that April and May 2026 now price instead of
  raising "no snapshot effective on or before".

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
