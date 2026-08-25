# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- The Home Assistant integration can optionally track what the meter actually
  moved. Name the cumulative grid-import and grid-export kWh entities under
  **Configure → Metered energy** — deliberately not part of initial setup,
  since pricing needs no meter and the counters are usually integrated after
  the tariff — and it adds running Energy Cost, Export Credit, and Net Cost
  entities for today and for the billing cycle to date, alongside Grid Import
  and Grid Export totals.

  The counters do not have to reset daily: each hour's energy comes from the
  recorder's own long-term statistics, which already absorb counter restarts
  and reload gaps, with the hour in progress read live off entity state.
  Readings are priced by `tariffkit.billing.BillEngine`, the same code that
  reconciles a printed statement, so the running figures carry time-of-use
  bucketing, the Energy Commission Tax, the baseline credit, the whole day's
  Base Services Charge, and the rule that exports before Permission To Operate
  earn nothing.

  Today's figures are the cycle's movement across today rather than a one-day
  bill, because parts of a bill are cumulative over a cycle rather than
  additive over its days -- the baseline allowance is granted per cycle and
  consumed in day order, so pricing a day alone grants it one day's allowance
  however much the cycle had banked. Gaps, overlaps and reconstructed intervals
  in the metered series are reported in `warnings` and clear
  `quality.complete`, so a recorder outage understates the figure loudly rather
  than quietly.

  A **Billing cycle start day** setting names the day of the month the meter is
  read, but it is only a fallback: where the profile carries imported
  statements the cycle boundary comes from the statements themselves, which is
  the only way to match a real bill -- PG&E reads on business days, so
  consecutive cycles open on the 29th, the 30th, the 1st and the 3rd. A
  `cycle_boundary` attribute reports which was used.

  Naming no entities creates none of these, leaving every existing entity
  byte-identical. One exception is worth knowing: an account profile imported
  from the CLI carries its own `meter_sources.ha` mapping, and that mapping is
  honoured, so such an entry gains the entities without anyone opening the
  form.

- A `tariffkit.backfill_usage` action prices metered history into long-term
  statistics, so cost and credit for days before the meters were configured
  appear in Home Assistant rather than only through the CLI. It writes external
  statistics under a `tariffkit:` namespace -- the shape `opower` uses for
  utility history -- which leaves the running entities' own series and the
  recorder's compilation of them untouched. One row per finished day, each being
  its cycle's movement across that day, so the days sum to their cycle exactly.
  Rerunning replaces the window rather than appending to it, which is how a
  corrected account history is picked up.

### Fixed
- `tariffkit account sync` now signs in before asking the portal for the
  statement list. A resumed session arrives with a live session cookie and no
  CSRF token, because the token is one-shot and deliberately not cached, so the
  first authenticated call failed -- either as a bare "the session token is
  stale" or, when the portal answered with an empty list instead of an error, as
  a silent "received 0 statement update(s)" against an account with 25
  statements. `apex`'s own recovery could not rescue it: it falls back to a
  forced re-login, which fails while already signed in because the login page
  redirects to the community and the token it carries belongs to the wrong
  Lightning app. `audit doctor` was unaffected because it calls `login()` first,
  which is what made the two disagree.

## [0.3.0] - 2026-08-22

### Added
- PG&E's complete active single-family residential lineup is now covered:
  generated E-1 and E-TOU-D snapshots join E-ELEC, E-TOU-C, and EV2-A.
  E-1 preserves tiered baseline billing while exposing the over-baseline
  marginal price, and E-TOU-D observes its weekday-only 5–8 p.m. peak and
  tariff holiday calendar.
- Generated, effective-dated D-CARE, D-MEDICAL, Rule 19 Medical Baseline, and
  E-RSMART data now drive residential program adjustments. SmartRate accepts
  explicit announced event dates and marks prices beyond the authoritative
  event horizon incomplete rather than guessing future events.
- Prices now decompose into a fixed set of chartable component groups —
  generation, distribution, transmission, surcharges, credits, and a catch-all
  other on the import side; generation, delivery, credits, and other on the
  export side. The groups sum back to the price they came from and do not vary
  with the tariff, supplier, or discount, so a chart built against them
  survives an account change. `ImportPrice.grouped()`, `ExportPrice.grouped()`,
  a `groups` key in every `to_dict` payload, and `tariffkit.components` expose
  them to library and REST callers.
- Home Assistant gains a sensor per component group in each direction, and the
  forecast's `rates` attribute carries the same roll-up per hour, so both the
  recorded past and the next 48 hours can be drawn as stacked charts -- one
  card each, because stacking recorded and forecast points together would
  double-count the current hour. The MQTT publisher publishes the same series
  with matching discovery payloads, each band carrying its price's quality
  flags.
- Home Assistant now exposes the AB 205 Base Services Charge as **Daily Fixed
  Charge** in `USD/day`. The unit keeps it out of the Energy dashboard's price
  pickers and out of any `USD/kWh` stack, which is why it can be published at
  all: it is a fixed daily amount, not a marginal price.

### Changed
- Home Assistant now offers every active PG&E residential schedule and
  Medical Baseline configuration while preserving existing profile and entity
  identities.

### Fixed
- Predbat attributes now use its `from` / `to` / `rate` contract for values
  already expressed in cents. Predbat no longer interprets TariffKit's cents as
  currency units and multiplies them by 100, while Pacific-midnight anchoring
  and complete 46-, 48-, and 50-slot tariff days remain unchanged.

## [0.2.3] - 2026-08-17

### Changed
- Pacific Gas and Electric now has the unambiguous machine identifier
  `pacific_gas_and_electric`, while Home Assistant and MQTT present `PG&E` or
  the full company name. The separate `pge` identifier correctly means Portland
  General Electric and is recognized but explicitly unsupported for pricing,
  preventing it from ever selecting California tariff data.
- Home Assistant now labels the export-minus-import calculation explicitly,
  translates time-of-use states for display, and presents the forecast horizon
  as **Rates Available Through**. Forecast metadata and a new **Rate Data
  Status** entity are grouped under diagnostics, where PTO date, export lock
  end, NBT vintage, tariff provenance, source, and quality flags explain the
  active rates without crowding the primary price controls.

### Fixed
- The PyPI project page now loads the TariffKit banner from an absolute URL
  instead of an unresolved repository-relative path.

## [0.2.2] - 2026-08-16

### Added
- Project documentation now uses a TariffKit banner built from the integration's
  existing icon and a provider-neutral electricity rate curve.
- Public contribution and support guidance now provides privacy-safe issue
  forms, private security routing, review ownership, a pull request checklist,
  community conduct expectations, and exact development checks. The guidance
  also makes the generated-data and repository-only audit boundaries explicit
  so public collaboration does not expose utility-account material or alter
  distribution guarantees.
- Pull requests now run GitHub's dependency review action with read-only
  permissions so vulnerable or disallowed dependency changes fail before merge.

### Changed
- **Python 3.14.2 and Home Assistant 2026.3.0 are now the supported floors.**
  The lockfile no longer carries the obsolete Home Assistant 2026.2 fallback,
  and CI audits the complete locked dependency graph with a pinned `pip-audit`
  release while retaining raw reports as failure artifacts. The Linux secrets
  extra and Home Assistant tests both inherit the same exact cryptography pin;
  an expiring policy requires each audit to report exactly its three known,
  unreachable advisories and rejects any additional finding.
- The README now explains that default HACS approval may take months and gives
  complete custom-repository installation steps for use during the review.
- Workflow actions are pinned to reviewed immutable commits, checkout credentials
  are not persisted, superseded runs are bounded by concurrency controls, and
  repository write access is isolated to the jobs that publish results.
- MQTT now rejects credentials over plaintext unless the operator explicitly
  allows insecure authentication for an isolated trusted network. Passwords
  also require a username, while anonymous plaintext publishing remains valid.

### Fixed
- Rate-sheet regeneration now scans trailing table cells with a linear parser,
  avoiding pathological regular-expression backtracking on malformed publisher
  text while preserving accepted dollar, decimal, negative, and change-marker
  forms.

## [0.2.1] - 2026-08-16

### Added
- **HACS releases now include a deterministic `tariffkit.zip` integration
  artifact.** HACS installs only the tracked component files, rooted directly
  in the integration directory, while the release pipeline validates the ZIP
  against its source, checksums it with the Python distributions, and attaches
  all artifacts before immutable publication. Dedicated HACS and hassfest
  checks also gate integration changes and prepare TariffKit for default-store
  submission.

### Fixed
- Home Assistant action descriptions now follow hassfest's current service
  schema: icons live in `icons.json`, target metadata is explicit, and the
  config-entry-only YAML schema is declared.

## [0.2.0] - 2026-08-16

### Added
- **Named, effective-dated account profiles** track tariff, supplier, baseline,
  export, and credential-set changes over a service agreement's lifetime.
  Pricing and billing resolve the settings in force at each timestamp, including
  cycles that cross an account transition. Profile writes are atomic,
  revision-checked, and locked against concurrent updates.
- **PG&E statement import and portal synchronization** can populate an account
  profile from printed facts. Proposed changes are reported as additions,
  confirmations, conflicts, or missing required values; a statement with a
  conflict cannot be partially applied. Statement support is available through
  the `statements` extra, while the account-specific audit harness remains
  repository-only.
- **Profile-scoped meter sources** store provider-neutral grid-import and
  grid-export mappings for Home Assistant and InfluxDB 3. The bill command can
  query either source directly, with explicit command options taking precedence
  over profile, environment, and global defaults.
- **Credential sets backed by the operating-system keyring** hold PG&E, Home
  Assistant, InfluxDB, and MQTT secrets outside configuration files and command
  arguments. Environment injection remains available for containers.
- **A complete billing layer** now covers interval netting, coverage warnings,
  baseline credits, fixed charges, taxes, export-credit buckets, annual true-up,
  and CCA cash-out. Credits retain their printed statement scope instead of
  being treated as one fungible balance.
- **Home Assistant and InfluxDB 3 interval sources** complement the Green Button
  reader. Home Assistant reads long-term statistics and prefers five-minute
  data; InfluxDB derives exact totals from cumulative-counter endpoints and
  spreads advances across the time in which they accrued.
- **E-TOU-C and EV2-A retail schedules**, complete PCIA and franchise-fee
  vintage tables, E-TOU-C baseline allowances, California's electrical-energy
  surcharge, Net Surplus Compensation data, and effective-dated PG&E and MCE
  rate snapshots extend pricing and billing beyond the original E-ELEC
  schedule.
- **Repository-only rate-data generators** rebuild every vendored dataset from
  its published source and read the rendered result back through runtime code
  before writing it. A weekly workflow checks export matrices, retail tariffs,
  ACC Plus, CCA cards, Net Surplus Compensation, holidays, and the state
  surcharge for upstream changes.
- **Home Assistant account-history flows** support staged initial setup and
  profile inspection, transition editing, statement import, and sanitized
  profile export. Stable profile-based config-entry identity survives tariff and
  supplier changes.
- **Home Assistant response actions** provide current or forecast rates in
  native and EMHASS shapes for caller-selected windows. Requests reject
  ambiguous DST-fold timestamps, misaligned windows, and horizons beyond seven
  days rather than guessing.
- **Home Assistant diagnostics and integration tests** cover config and options
  flows, migration, entities, actions, Energy dashboard compatibility,
  effective-dated provenance, DST handling, and opt-in Predbat output.
  Diagnostics deliberately omit account history, observations, credentials,
  and meter mappings.
- **Pure interoperability adapters** generate EMHASS, Predbat, and generic slot
  payloads from a `PriceCurve`, shared by Home Assistant and MQTT without either
  integration becoming a dependency.
- **Request-scoped REST configuration and account selection** allow callers to
  price one request against validated settings or a named profile. Unknown
  configuration keys and credential fields are rejected.
- **A bind-mounted Home Assistant development stack** runs the custom component
  and local package source in a real container without publishing a wheel.
- **A build-once release process and maintainer runbook** synchronize Python,
  lockfile, Home Assistant, changelog, and documentation versions. One validated
  wheel/sdist pair moves through optional TestPyPI staging, protected PyPI
  approval, PEP 740 attestations, and an immutable GitHub release.

### Changed
- **The project is now TariffKit.** The distribution, import package, CLI,
  configuration directory, environment prefix, MQTT namespace, repository
  links, and Home Assistant domain use the utility-neutral `tariffkit` identity.
  The initial data-provider scope remains PG&E and California.
- **Packaging remains one public distribution in one repository.** Optional
  features use extras and lazy imports, leaving the default runtime
  dependency-free. Rate-data tooling and the account-specific audit harness are
  excluded from wheel and sdist but retain strict lint, typing, and test
  coverage.
- **Python 3.14 is the minimum**, and Home Assistant 2026.3.0 is therefore the
  integration floor. Development dependencies use PEP 735 groups, package
  licensing uses an SPDX expression, and installed metadata is the runtime
  version source.
- **Home Assistant installs the exact matching TariffKit release** instead of
  vendoring package source. Its config flow asks only questions made relevant
  by earlier answers and separates account history from forecast and Predbat
  options.
- **Home Assistant entities are lean and recorder-safe.** Import and export
  price sensors remain compatible with the Energy dashboard, a timestamp entity
  reports forecast coverage, and large forecast or optimizer payloads are not
  recorded every minute. The fixed daily charge is no longer represented as a
  marginal price sensor.
- **EMHASS forecasts are requested through an action and Predbat output is
  opt-in.** Neither large payload is computed and attached to every entity when
  unused.
- **Pricing provenance and quality are preserved end to end.** Core, CLI, REST,
  MQTT, and Home Assistant outputs retain `locked`, `exact`, and `complete`
  states and describe contiguous provenance segments across effective-date
  boundaries.
- **Green Button names now describe the format rather than its container.**
  `read_green_button`, `GreenButtonLayout`, and `--source green-button` replace
  generic CSV terminology; the legacy source spelling remains accepted.
- **Strict typing now covers the Home Assistant custom component and all
  repository tools**, while Ruff enforces function annotations throughout the
  repository.

### Fixed
- Production publishing no longer inherits a false implicit failure from the
  intentionally skipped optional TestPyPI job. The explicit release dependency
  checks now permit protected PyPI and GitHub publication only after the build
  and draft jobs themselves succeed.
- Account-profile updates no longer race between revision checks and filesystem
  mutation, statement-derived histories reject gaps and overlaps, imported Home
  Assistant profiles cannot replace an entry's stable identity, and MQTT
  profile selection respects environment precedence.
- Baseline credits use each day and vintage's own rate, pre-PTO exports earn no
  compensation, string supplier values are normalized before branching, and
  historical pricing never borrows data from a future vintage.
- CCA generation rates are selected by PG&E schedule instead of silently using
  E-ELEC values for every plan. PCIA, franchise-fee, MCE generation, cost-relief,
  and premium data are resolved from their actual effective vintages.
- Billing coverage and interval stepping use absolute time across both DST
  transitions. Repeated autumn timestamps are disambiguated during ingest, rate
  points remain contiguous, and MQTT no longer sleeps through the second 01:00
  hour.
- Green Button ingestion handles account preambles, split date/time columns,
  unit-suffixed headers, explicit offsets, and repeated autumn wall times.
  Home Assistant and InfluxDB sources report implausible resets and missing
  coverage instead of inventing plausible energy.
- Export-rate lookups use each vintage's own holiday calendar and surface
  publisher drift beyond the verified exact range rather than silently claiming
  exact future values.
- Generator parsing no longer drops a PCIA row joined to the next page header,
  and all generated files are validated through independent runtime readers
  before replacing vendored data.

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

[Unreleased]: https://github.com/eman/tariffkit/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/eman/tariffkit/releases/tag/v0.3.0
[0.2.3]: https://github.com/eman/tariffkit/releases/tag/v0.2.3
[0.2.2]: https://github.com/eman/tariffkit/releases/tag/v0.2.2
[0.2.1]: https://github.com/eman/tariffkit/releases/tag/v0.2.1
[0.2.0]: https://github.com/eman/tariffkit/releases/tag/v0.2.0
