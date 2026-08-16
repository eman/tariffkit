# REST API

A read-only HTTP service over the same engine. Useful for Node-RED, dashboards,
or anything on another host.

## Setup

```bash
pip install 'tariffkit[web]'
tariffkit serve                        # 127.0.0.1:8000
tariffkit serve --host 0.0.0.0 --port 8080
```

Every response is pure computation over vendored data: no upstream to call, no
rate limit, nothing to cache-invalidate. Interactive docs at `/docs`.

## Endpoints

| | |
|---|---|
| `GET /v1/price/now` | Current hour |
| `GET /v1/price/at?ts=<ISO8601>` | A specific hour; `ts` **must** carry a UTC offset |
| `GET /v1/forecast?hours=48` | Hourly curve, `hours` 1–8760 |
| `GET /v1/meta` | Loaded data, vintage, lock end, provenance |
| `GET /v1/healthz` | Liveness |

```bash
curl -s localhost:8000/v1/price/now | jq
curl -s 'localhost:8000/v1/price/at?ts=2026-09-15T19:00:00-07:00' | jq '.export.total'
curl -s 'localhost:8000/v1/forecast?hours=24' | jq '.points[] | {start, export: .export.total}'
```

A naive timestamp returns 422 rather than being guessed at. A timestamp outside
the vendored data returns 404.

### Request-scoped configuration

POST to the same pricing paths when a caller needs to price a different service
agreement without changing server state:

```bash
curl -s localhost:8000/v1/price/at \
  -H 'content-type: application/json' \
  -d '{
    "ts": "2026-09-15T19:00:00-07:00",
    "config": {
      "tariff": "EV2-A",
      "supplier": "cca",
      "interconnection_year": 2026,
      "pto_date": "2026-06-03",
      "cca": {"name": "MCE", "rate_card": "mce", "pcia_vintage": 2011}
    }
  }'
```

`POST /v1/price/now`, `POST /v1/price/at`, `POST /v1/forecast`, and
`POST /v1/meta` accept the same `config` object as `Config.from_dict()`.
Configuration is validated, used for that request, and discarded. Unknown keys
return 422, so credentials cannot accidentally be accepted or persisted by the
pricing service.

## Response shape

```json
{
  "start": "2026-09-15T19:00:00-07:00",
  "end": "2026-09-15T20:00:00-07:00",
  "import": { "total": 0.55214, "season": "summer", "period": "peak",
              "components": { "...": 0.0 }, "complete": true },
  "export": { "total": 0.60385, "vintage": "NBT26", "day_type": "Weekday",
              "components": { "generation": 0.59312, "delivery": 0.00193,
                              "acc_plus": 0.0088 },
              "locked": true, "complete": true, "exact": true },
  "spread": 0.05171
}
```

Check the flags before acting on a price:

- `complete: false`: CCA generation rates are unconfigured; this is
  delivery-only and understates the real figure.
- `locked: false`: past your nine-year rate lock; PG&E publishes these for
  illustration only.
- `exact: false`: far-future year where PG&E's own hour labels drift.

## Custom config

```bash
tariffkit --config /etc/tariffkit/config.toml serve
```

Or build the app yourself:

```python
from tariffkit import Config
from tariffkit.web import create_app

app = create_app(Config.from_toml("/etc/tariffkit/config.toml"))
```

```bash
uvicorn myapp:app --host 0.0.0.0 --port 8000 --workers 2
```

## Run as a service

```ini
# /etc/systemd/system/tariffkit-web.service
[Unit]
Description=tariffkit REST API
After=network-online.target

[Service]
Type=simple
User=tariffkit
Environment=XDG_CONFIG_HOME=/etc/tariffkit
ExecStart=/opt/tariffkit/.venv/bin/tariffkit serve --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Docker

```dockerfile
FROM python:3.14-slim
RUN pip install --no-cache-dir 'tariffkit[web]'
EXPOSE 8000
CMD ["tariffkit", "serve", "--host", "0.0.0.0"]
```

```bash
docker run -p 8000:8000 \
  -v /etc/tariffkit:/config \
  -e XDG_CONFIG_HOME=/config \
  tariffkit
```

Mount the config file or provide `TARIFFKIT_CCA_JSON` for complete CCA settings.

## Security

There is no authentication. Bind to localhost, or put it behind a reverse proxy
or firewall. It exposes your rate plan and interconnection details, and it is
read-only, but it is not written to face the internet. Request bodies must never
contain credentials; this API has no credential-backed operation.

## Named account profiles

`GET /v1/meta`, `POST /v1/meta`, and every `POST` pricing endpoint can price
against a [named account profile](accounts.md) instead of a stateless
`Config`:

```bash
curl -s localhost:8000/v1/meta -X POST -H 'content-type: application/json' \
  -d '{"profile": "home"}' | jq '.account_profile, .account_effective.tariff'
```

When a profile is active, `describe()` adds `account_profile` (its name) and
`account_effective` (the resolved `Config.to_dict()` for the requested
moment) to the usual `/v1/meta` fields.

**Server-wide default.** `create_app(profile_name=..., profile_repository=...,
config_path=...)` selects a profile for every request that does not name one
itself, resolved the same way as the CLI's implicit default (env vars, then
`[account] default_profile` in `config.toml`) when `profile_name` is not
passed explicitly. `tariffkit serve` wires this up automatically; building
the app yourself with `create_app(Config(...))` (a `config` positional
argument) opts out of profile resolution entirely for that server, the same
way `--config` does on the CLI.

**Per-request selection.** Pass `profile` (or `account` — the two must agree
if both are given) alongside `ts`/`hours` instead of `config`:

```bash
curl -s localhost:8000/v1/price/now -X POST -H 'content-type: application/json' \
  -d '{"profile": "home"}'
```

`config` and `profile`/`account` are mutually exclusive per request (422
`"choose either config or profile"`), the same restriction as the CLI's
`--account`/`--config`. An unknown or unreadable profile name returns
`404 {"detail": "profile unavailable"}` — deliberately identical whether the
name does not exist, is malformed, or fails to load, so a request cannot
enumerate what profiles exist on the server.

**Nothing here can change a profile.** There is no endpoint to list, create,
update, import, export, or delete one, and none accepts a PDF or a
credential — those are exclusively `tariffkit account ...` and Home
Assistant's options flow. The REST surface only ever *reads* a profile
already managed elsewhere. See [Named account profiles](accounts.md) for how
one is created and kept current.
