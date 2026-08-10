# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

### Notes
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
