# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-08-09

### Added
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
