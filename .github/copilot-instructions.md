# Copilot instructions for `nem-rates`

## Build, test, and lint

Use the same toolchain and command style as CI (`uv` + Python 3.11+).

```bash
# Install dev environment (all extras + test/lint/type tools)
uv sync --all-extras

# Lint
uv run ruff check .
uv run ruff format --check .

# Type-check
uv run mypy

# Full test suite
uv run pytest --cov=nem_rates --cov-report=term-missing

# Run one test file
uv run pytest tests/test_engine.py

# Run one test function
uv run pytest tests/test_engine.py::test_describe_reports_provenance

# Run one test method in a test class
uv run pytest tests/test_engine.py::TestDaylightSaving::test_fall_back_day_has_25_hours

# Run tests by keyword / focus area
uv run pytest -k "forecast and not slowdata"

# Run integration-surface tests (CLI/MQTT/API)
uv run pytest tests/test_integrations.py

# Build distributables
uv build

# Confirm vendored data is included in built wheel (CI parity)
python -m zipfile -l dist/*.whl | grep 'nem_rates/data/export/pge/nbt26.json.gz'
```

## Updating vendored rate data

Every vendored dataset is generated from the document that publishes it. None is
hand-transcribed. Never hand-edit a generated file; regenerate it.

### Export rates (NBT matrices + holiday calendar)

`src/nem_rates/data/export/pge/*.json.gz`, `holidays.toml`, and `manifest.json`
come from PG&E's published CSV archive. It is 843 MB, so it has its own entry
point rather than sharing the PDF-driven one.

```bash
# Regenerate from a local copy of the archive
python -m nem_rates.regen.export --zip /path/to/PGE-Solar-Billing-Plan-Export-Rates.zip

# Or download it directly
python -m nem_rates.regen.export --download

# CI-style check: exit 1 if upstream has moved, without writing anything
python -m nem_rates.regen.export --download --check
```

After regenerating, run `uv run pytest tests/test_export_golden.py`. It
round-trips the new matrices against known-good rows sampled from PG&E's own
files (including both DST transitions and a holiday), so a bad collapse fails
loudly rather than shipping silently.

### Retail tariffs, ACC Plus, CCA cards, NSC, and the state surcharge: `nem-rates regen`

`src/nem_rates/data/{tariff,export/*/acc_plus,cca,nsc,tax}/**.toml` are
**generated** from published documents by `nem_rates.regen`. Do not hand-edit
them; each names the document it came from in a header comment.

```bash
nem-rates regen                                # rebuild every dataset from live documents
nem-rates regen --check                        # exit 1 if a publisher moved, writing nothing
nem-rates regen tariff --for-date 2025-12-15   # rebuild a superseded vintage
```

Nothing is written unless the rendered file survives being read back by the
library code that will consume it. A generator writes key names and the library
reads them with a second, independent set of literals — two encodings of one
schema — so the check is what keeps them from drifting apart silently.

Rates are **effective-dated**: a dataset is a directory of `<effective>.toml`,
and `nem_rates.data.versioned` resolves the version in force on a date — the
latest effective on or before it, raising rather than borrowing when a date
predates every vintage. Add a new dated file rather than editing the current
one, or old bills silently reprice at today's rates.

`RetailTariff` / `load_snapshot` (`src/nem_rates/tariff/retail.py`) read those
snapshots. After vendored data changes, run `uv run pytest` in full:
`tests/test_engine.py`, `tests/test_integrations.py` and
`tests/test_mqtt_publisher.py` all assert specific dollar figures derived from
it, and a CCA card should be reconciled against a real bill as the MCE tests do.

## High-level architecture

- The core API is `RateEngine` (`src/nem_rates/engine.py`), which composes:
  - `RetailTariff` for **import** pricing (`src/nem_rates/tariff/retail.py`), covering E-ELEC, E-TOU-C and EV2-A
  - `NbtExportRates` for **export** pricing (`src/nem_rates/export/nbt.py`)
- Runtime pricing is offline and table-driven. Data is vendored under `src/nem_rates/data/`; runtime code does not call external services.
- `nem_rates.regen` regenerates every vendored dataset from its published source, and `nem_rates.regen.export` is the normalization pipeline that collapses PG&E's large hourly CSVs into compact vendored matrices, with exactness checks and provenance metadata.
- `nem_rates.data.versioned` resolves effective-dated data: the version in force is the latest effective on or before the priced date, and a date before every vintage raises rather than borrowing.
- `audit/` is a harness, outside the package, that reconciles computed bills against real PG&E statements. It is not shipped in the wheel.
- `Config`/`CcaConfig` (`src/nem_rates/config.py`) resolve tariff/vintage/CCA settings from defaults, TOML, and `NEM_RATES_*` env overlays.
- `models.py` defines the pricing contracts (`ImportPrice`, `ExportPrice`, `PricePoint`, `PriceCurve`) shared across surfaces.
- Delivery surfaces are thin wrappers around the same engine:
  - CLI (`src/nem_rates/cli.py`)
  - REST API (`src/nem_rates/web/app.py`)
  - MQTT publisher + Home Assistant discovery (`src/nem_rates/mqtt/`)
  - Home Assistant custom component (`custom_components/nem_rates/`)
- Billing (`src/nem_rates/billing/`) is a separate pure layer that consumes interval readings and engine outputs to compute decomposed cycle totals.

## Repository conventions

- **Timezone handling is strict and explicit**: public APIs expect timezone-aware datetimes; conversions are normalized to Pacific time; DST boundaries are handled via absolute-time stepping.
- **Do not silently “fill in” missing pricing inputs**: incomplete CCA configuration should propagate via `complete=False` flags on price objects rather than fabricated totals.
- **Preserve pricing quality flags** on outputs and integrations: `locked`, `exact`, and `complete` are intended for downstream decision logic and should not be dropped.
- **Keep fixed daily charges separate from per-kWh marginal prices** (`daily_fixed_charge()` is separate by design).
- **Errors are surfaced, not hidden**: domain failures use typed exceptions (`NemRatesError` family); API/CLI layers convert them to user-facing errors/status codes.
- **Core package remains dependency-light**: optional features (`web`, `mqtt`) use extras and lazy imports with explicit runtime messages when extras are missing.
- **Typing and linting are intentionally strict** in `src/nem_rates` (`mypy --strict`, broad Ruff rule set). Follow existing typing quality rather than introducing `Any`-heavy shortcuts.
- **Home Assistant integration has deliberate lint exceptions** in Ruff config to match HA conventions; avoid “normalizing” HA files to core-package naming/signature patterns.

## MCP server guidance (optional)

If MCP servers are available in your Copilot environment, prioritize:

- **GitHub MCP** for issue/PR/review workflows tied to this repo.
- **Browser/API automation MCP (for example Playwright)** when validating the REST surface end-to-end (`nem-rates serve` + `/v1/*` endpoints), especially for reproducible integration checks.
