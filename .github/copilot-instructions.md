# Copilot instructions for `nem-rates`

## Build, test, and lint

Use the same toolchain and command style as CI (`uv` + Python 3.14).

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

# The audit harness has its own suite, NOT covered by the command above
uv run pytest audit/tests

# Build distributables
uv build

# Confirm vendored data is included in built wheel (CI parity)
python -m zipfile -l dist/*.whl | grep -q 'nem_rates/data/export/pge/nbt26.json.gz'
python -m zipfile -l dist/*.whl | grep -q 'nem_rates/data/holidays.toml'

# ...and that the audit harness is not (CI parity)
! python -m zipfile -l dist/*.whl | grep -q '^audit/'
! tar -tzf dist/*.tar.gz | grep -q '/audit/'
```

`testpaths` is `["tests"]`, so `uv run pytest` does not reach `audit/tests`. CI
runs it as a second invocation rather than adding a testpath, because `audit/`
is absent from the sdist and pytest errors on a testpath that does not exist.
Run both before calling a change green. `mypy` does cover `audit/` — `files`
lists it — so a type error there fails CI even though the tests would not.

Three pytest markers are declared (`--strict-markers` is on, so an undeclared
marker is an error, and new ones go in `pyproject.toml`):

| marker | meaning |
|---|---|
| `slowdata` | needs the full 843 MB upstream PG&E dataset |
| `statements` | reads real statement PDFs, which are never committed |
| `live` | signs in to the PG&E portal; **never run in CI** |

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

## The audit harness (`audit/`)

`audit/` reconciles bills computed by this library against real PG&E statements.
It is the check that found every defect the library has shipped, so a change to
pricing, billing, or vendored data is not really validated until it still
reconciles. Read `audit/README.md` before touching it.

```bash
python -m audit parse ~/Desktop/PGE_20260205.pdf      # what the bill says
python -m audit reconcile ~/Desktop/PGE_*.pdf         # against what we compute
python -m audit doctor                                # preflight: config, data, endpoints
uv run python -m audit run --since 2025-11-01 --until 2026-08-31
```

- **It is deliberately outside the package and must never ship.** It parses one
  utility's paper for one account and needs that account's login. CI asserts its
  absence from both wheel and sdist; do not "fix" that by packaging it.
- **It is held to the same strictness as `src/`** — `mypy --strict` and the full
  Ruff rule set — because a harness that is trusted when it reports a
  discrepancy has to be trustworthy itself. `known-first-party` in the isort
  config lists `audit` alongside `nem_rates` for the same reason.
- **Exit codes are load-bearing and distinct**: `0` all reconciled, `1` a real
  disagreement (mismatch, unmapped line, or unmapped component), `2` the check
  could not be performed. Do not collapse `1` and `2` — a broken harness must
  not look like a billing error.
- **Three residuals are reported separately and never merged**: an unmapped
  printed line, an unmapped computed component, and a genuine mismatch.
- **Secrets and statements stay out of the repo.** `audit/account.toml` (from
  `account.example.toml`) is gitignored, `*.pdf` is gitignored, statements are
  deleted after a run unless `--keep-statements`, and interval data comes from
  InfluxDB via `INFLUXDB3_*` in `.env`. `NEM_RATES_STATEMENT_DIR` points at
  existing statements instead of downloading.
- **A cycle spanning an account change is refused, not guessed.** `account.toml`
  dates the account's schedule, supplier, baseline territory, and PCIA vintage,
  because `Config` describes one moment.
- **Do not close a residual by substituting better inputs.** Known differences
  trace to gaps in the meter record and are reported rather than repaired;
  `reconcile/attribution.py` re-prices a mismatching line from the statement's
  own kWh to separate a rate error from a metering one. Making the numbers agree
  by improving the inputs would make the agreement worthless.
- `pge/PORTAL.md` records what the portal actually is — a Salesforce Experience
  Cloud community with no REST endpoints, serving PDFs base64-encoded inside
  Aura JSON. Read it before writing anything that talks to it.

## High-level architecture

- The core API is `RateEngine` (`src/nem_rates/engine.py`), which composes:
  - `RetailTariff` for **import** pricing (`src/nem_rates/tariff/retail.py`), covering E-ELEC, E-TOU-C and EV2-A
  - `NbtExportRates` for **export** pricing (`src/nem_rates/export/nbt.py`)
- Runtime pricing is offline and table-driven. Data is vendored under `src/nem_rates/data/`; runtime code does not call external services.
- `nem_rates.regen` regenerates every vendored dataset from its published source, and `nem_rates.regen.export` is the normalization pipeline that collapses PG&E's large hourly CSVs into compact vendored matrices, with exactness checks and provenance metadata.
- `nem_rates.data.versioned` resolves effective-dated data: the version in force is the latest effective on or before the priced date, and a date before every vintage raises rather than borrowing.
- `audit/` is a harness, outside the package, that reconciles computed bills against real PG&E statements. It is not shipped in the wheel — see the section above.
- `Config`/`CcaConfig` (`src/nem_rates/config.py`) resolve tariff/vintage/CCA settings from defaults, TOML, and `NEM_RATES_*` env overlays. `cca.py` holds the CCA rate-card model; `timeutil.py` holds the Pacific-time and DST-stepping helpers everything else uses.
- `models.py` defines the pricing contracts (`ImportPrice`, `ExportPrice`, `PricePoint`, `PriceCurve`) shared across surfaces.
- Delivery surfaces are thin wrappers around the same engine:
  - CLI (`src/nem_rates/cli.py`) — subcommands `now`, `forecast`, `info`, `bill`, `mqtt`, `regen`, `serve`
  - REST API (`src/nem_rates/web/app.py`)
  - MQTT publisher + Home Assistant discovery (`src/nem_rates/mqtt/`)
  - Home Assistant custom component (`custom_components/nem_rates/`)
- Billing (`src/nem_rates/billing/`) is a separate pure, stdlib-only layer that consumes interval readings and engine outputs: `engine.py` decomposes a cycle, `netting.py` finds gaps/overlaps and nets intervals, `ledger.py` tracks export credit buckets, `trueup.py` handles annual true-up and cash-out (PG&E and MCE).
- `nem_rates.sources/` turns external records of metered energy into `IntervalReading`s for the billing layer, and is where dependency-carrying I/O lives so billing can stay stdlib-only: `greenbutton.py` (PG&E CSV, best timing, worst totals), `homeassistant.py` (long-term statistics, biases energy forward across bucket boundaries), `influx.py` (raw counter samples, exact totals and pro-rata timing), `pge.py` (authenticated session and Green Button download). The differences between them are about *when* energy is recorded, which time-of-use pricing cares about — do not treat them as interchangeable.
- `nem_rates.interop/` publishes rates in shapes other energy systems already read (`emhass.py`, `predbat.py`, `slots.py`). These are pure functions over a `PriceCurve` so the HA component and the MQTT publisher share one implementation and it stays testable without either — keep new adapters that way.

## Repository conventions

- **Timezone handling is strict and explicit**: public APIs expect timezone-aware datetimes; conversions are normalized to Pacific time; DST boundaries are handled via absolute-time stepping.
- **Do not silently “fill in” missing pricing inputs**: incomplete CCA configuration should propagate via `complete=False` flags on price objects rather than fabricated totals.
- **Preserve pricing quality flags** on outputs and integrations: `locked`, `exact`, and `complete` are intended for downstream decision logic and should not be dropped.
- **Keep fixed daily charges separate from per-kWh marginal prices** (`daily_fixed_charge()` is separate by design).
- **Errors are surfaced, not hidden**: domain failures use typed exceptions (`NemRatesError` family); API/CLI layers convert them to user-facing errors/status codes.
- **Core package remains dependency-light**: optional features (`web`, `mqtt`) use extras and lazy imports with explicit runtime messages when extras are missing.
- **Typing and linting are intentionally strict** in `src/nem_rates` (`mypy --strict`, broad Ruff rule set). Follow existing typing quality rather than introducing `Any`-heavy shortcuts.
- **Home Assistant integration has deliberate lint exceptions** in Ruff config to match HA conventions; avoid “normalizing” HA files to core-package naming/signature patterns.
- **Python 3.14 is the floor**, and CI runs exactly that one version rather than a matrix — a matrix that spanned versions nobody develops on could only catch regressions where they did not matter. Raising it also raises the Home Assistant floor in `hacs.json` (currently 2026.3.0), and the two move together.
- **Comments explain why, not what.** Config files here carry the reasoning for non-obvious choices (why `audit` is in `known-first-party`, why the wheel check is pinned, why the rate-data job gates on step outcomes rather than `failure()`). Preserve those when editing, and add one when making a choice a reader would otherwise undo.

## Changelog and docs

- **`CHANGELOG.md` is updated in the same commit as the change.** Substantive
  changes go under `## [Unreleased]` in `Added` / `Changed` / `Fixed`, written as
  prose that says what changed and why it mattered — the existing entries are
  the model, not one-line bullets.
- **User-facing behaviour changes reach `docs/`.** The set is
  `configuration.md`, `library.md`, `billing.md`, `mqtt.md`, `web.md`,
  `home-assistant.md`, `data.md`, indexed from a table in `README.md`. A new
  flag, endpoint, sensor, or dataset that only exists in code is not finished.

## CI

Two workflows, in `.github/workflows/`:

- `ci.yml` — on push to `main` and every PR. Lint, format check, `mypy`, the
  main suite with coverage, then `audit/tests`; a separate `build` job asserts
  the vendored data is inside the wheel and the audit harness is inside neither
  wheel nor sdist.
- `rate-data-check.yml` — weekly and on demand. Runs both `--check`
  regenerators against live publishers and opens a single deduplicated
  `rate-data`-labelled issue when upstream has moved. It needs `issues: write`,
  gates on the two comparison steps rather than `failure()` so an unrelated job
  failure cannot file a false "out of date" issue, and skips-with-a-note rather
  than fails on sources a script cannot fetch. A permanently red check is one
  everyone learns to ignore, so preserve that distinction.

## MCP server guidance (optional)

If MCP servers are available in your Copilot environment, prioritize:

- **GitHub MCP** for issue/PR/review workflows tied to this repo.
- **Browser/API automation MCP (for example Playwright)** when validating the REST surface end-to-end (`nem-rates serve` + `/v1/*` endpoints), especially for reproducible integration checks.
