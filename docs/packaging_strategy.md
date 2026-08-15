# Python Packaging Strategy

Based on the architecture of the `src/nem_rates` directory and the `pyproject.toml` extras, the project is currently structured as a **monolithic package with optional dependencies (`extras`)**.

For a project of this size, that is a pragmatic and robust starting point while it remains private or highly specific to a single use case. However, when generalizing this project for a broader public release, it is recommended to split it into multiple independent packages. This minimizes the dependency footprint for downstream consumers (like the Home Assistant integration) and creates clearer domain boundaries.

Here is the recommended architecture for splitting the packages:

### 1. `nem-rates-core` (The Engine)
* **Contents:** `models.py`, `engine.py`, `tariff/`, `config.py`, `billing/`, `timeutil.py`, and `cca.py`.
* **Purpose:** This is the pure, dependency-free (or minimal dependency) state machine and calculator. It accepts timestamps, consumption data, and configuration, and outputs costs, prices, and bills. 
* **Why split it?** Other developers (and the Home Assistant custom component) only need the core business logic to calculate prices locally. They should not be forced to install web server dependencies, MQTT clients, or PDF scrapers just to calculate a tariff. 

### 2. `nem-rates-client` (Data Fetching & API)
* **Contents:** `sources/`, `pge/`, `fetch`, and `interop/`.
* **Purpose:** Handles the messy reality of talking to external services. It manages authentication with the PG&E portal, fetching usage data, parsing statements, and pulling down upstream rate data.
* **Why split it?** Fetching data from utility companies is notoriously brittle and requires heavier HTTP dependencies (like `httpx`). Keeping this separate ensures that if the PG&E portal changes and scraping breaks, the core rating engine remains perfectly valid and stable.

### 3. `nem-rates-server` / `nem-ratesd` (The Daemon/Services)
* **Contents:** `web/` (FastAPI), `mqtt/`, `cli.py`, and `export/` (InfluxDB).
* **Purpose:** The standalone application layer for users who *do not* use Home Assistant but want to integrate NEM 3.0 data into Node-RED, Grafana, or a custom smart home stack.
* **Why split it?** This layer requires heavyweight dependencies (`fastapi`, `uvicorn`, `paho-mqtt`, etc.). Users building simple scripts or integrations do not need a full ASGI web server in their environment.

### 4. Build Tools (`regen/`)
Code that rebuilds the vendored rate data by scraping PG&E PDFs (`regen/`, using `pypdf`) should not be published in the end-user packages at all. This is strictly an internal pipeline for the repository maintainers to generate the JSON/CSV data that ships statically with `nem-rates-core`.

## Conclusion

Utilizing `pyproject.toml` extras (e.g., `nem-rates[web, mqtt]`) is perfectly acceptable while the project is private. However, prior to a public launch, extracting at least a **pure, lightweight core package** (`nem-rates-core`) will significantly improve adoption by the Home Assistant ecosystem and other Python developers by eliminating unnecessary dependencies.
