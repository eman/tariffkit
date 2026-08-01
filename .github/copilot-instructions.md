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

Rate data maintenance commands used by docs/CI:

```bash
# Refresh vendored upstream export-rate data
python tools/regen_data.py --download

# Check whether vendored data drifted upstream (non-zero exit on drift)
python tools/regen_data.py --download --check
```

## High-level architecture

- The core API is `RateEngine` (`src/nem_rates/engine.py`), which composes:
  - `EelecTariff` for **import** pricing (`src/nem_rates/tariff/eelec.py`)
  - `NbtExportRates` for **export** pricing (`src/nem_rates/export/nbt.py`)
- Runtime pricing is offline and table-driven. Data is vendored under `src/nem_rates/data/`; runtime code does not call external services.
- `tools/regen_data.py` is the normalization pipeline that collapses PG&E’s large hourly CSVs into compact vendored matrices, with exactness checks and provenance metadata.
- `Config`/`CcaConfig` (`src/nem_rates/config.py`) resolve tariff/vintage/CCA settings from defaults, TOML, and `NEM_RATES_*` env overlays.
- `models.py` defines the pricing contracts (`ImportPrice`, `ExportPrice`, `PricePoint`, `PriceCurve`) shared across surfaces.
- Delivery surfaces are thin wrappers around the same engine:
  - CLI (`src/nem_rates/cli.py`)
  - REST API (`src/nem_rates/web/app.py`)
  - MQTT publisher + Home Assistant discovery (`src/nem_rates/mqtt/`)
  - Home Assistant custom component (`custom_components/nem_rates/`)
- Billing (`src/nem_rates/billing/`) is a separate pure layer that consumes interval readings and engine outputs to compute decomposed cycle totals.

## Key repository conventions

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
