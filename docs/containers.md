# Containers

This page has two purposes:

- a working Home Assistant development environment for testing the custom
  component from this checkout;
- the proposed deployment model for the REST API and MQTT publisher.

## Test the Home Assistant integration

The root [`compose.yaml`](../compose.yaml) builds the official Home Assistant
image with the current TariffKit distribution installed, then bind mounts:

| Host path | Container path | Purpose |
|---|---|---|
| `dev/home-assistant/` | `/config` | Writable Home Assistant configuration and state |
| `custom_components/` | `/config/custom_components` | Live custom-component source |
| `src/` | `/workspace/src` | Live TariffKit library and vendored rate data |

The image installation supplies the `tariffkit==0.4.1` distribution metadata
required by the integration manifest. `PYTHONPATH=/workspace/src` makes Python
load the bind-mounted source, so edits under either `custom_components/` or
`src/` are tested without rebuilding the image.

Start Home Assistant:

```bash
docker compose up --build
```

Open <http://localhost:8123>, complete Home Assistant's local onboarding, then
add **TariffKit** under **Settings → Devices & services**. Home Assistant writes
its generated state to `dev/home-assistant/`; the directory's `.gitignore`
keeps that machine-local state out of Git.

Python modules are loaded when an integration starts. After changing component
or library code, restart the container:

```bash
docker compose restart homeassistant
docker compose logs -f homeassistant
```

Rebuild only when package metadata, dependencies, the base Home Assistant
version, or the development Dockerfile changes:

```bash
docker compose up --build --force-recreate
```

The defaults can be overridden without editing Compose:

```bash
HA_VERSION=2026.8.1 HA_PORT=18123 TZ=America/Los_Angeles docker compose up --build
```

Pin `HA_VERSION` when reproducing a version-specific defect. The default
`stable` tag is appropriate for day-to-day compatibility testing.

To discard the local Home Assistant instance, stop the stack and delete the
generated files under `dev/home-assistant/`, preserving `.gitignore` and
`configuration.yaml`.

## Proposed API and MQTT deployment

Use one immutable TariffKit runtime image for both processes. Install the
`web` and `mqtt` extras in that image, run it as a non-root user, and select
the process with the Compose `command`. This keeps one version, SBOM, and
release artifact while preserving independent restart and scaling policies.

The proposed service layout is:

```yaml
services:
  api:
    image: ghcr.io/eman/tariffkit:0.4.1
    command: ["tariffkit", "serve", "--host", "0.0.0.0", "--port", "8000"]
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      XDG_CONFIG_HOME: /config
    volumes:
      - ./tariffkit-config:/config:ro
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/healthz')"]
      interval: 30s
      timeout: 5s
      retries: 3

  mqtt:
    image: ghcr.io/eman/tariffkit:0.4.1
    command: ["tariffkit", "mqtt", "--port", "8883", "--tls"]
    environment:
      XDG_CONFIG_HOME: /config
      TARIFFKIT_MQTT_BROKER: mqtt.example
      TARIFFKIT_MQTT_USERNAME: tariffkit
      TARIFFKIT_MQTT_PASSWORD: ${TARIFFKIT_MQTT_PASSWORD:?set in the deployment environment}
    volumes:
      - ./tariffkit-config:/config:ro
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
    restart: unless-stopped
    stop_grace_period: 30s
```

### Configuration boundaries

Mount the XDG configuration root, not a single file. TariffKit resolves
`/config/tariffkit/config.toml` and named profiles below
`/config/tariffkit/accounts/`. Both services only need read access at runtime.
Do not mount a developer's home directory, repository `.env`, keyring, or audit
configuration into either container.

The API should bind to loopback as shown, or sit behind an authenticated reverse
proxy. It has no built-in authentication and should not be exposed directly to
the internet.

The MQTT publisher requires outbound broker access but publishes no listening
port. Authenticated connections must use TLS, conventionally on port 8883.
Inject its password from the deployment platform's secret store. Compose
environment interpolation is the currently supported path; native Docker secret
files would require adding a `TARIFFKIT_MQTT_PASSWORD_FILE` setting before
adopting this as a production stack. A broker confined to an isolated trusted
LAN may set `TARIFFKIT_MQTT_ALLOW_INSECURE_AUTH=true`, but that explicit escape
hatch sends credentials without transport encryption and must not be used on an
untrusted network.

### Image and release requirements

Before publishing the runtime image:

1. Build from `python:3.14-slim` and install the released wheel with
   `tariffkit[web,mqtt]`.
2. Create and switch to a fixed, unprivileged UID in the image.
3. Pin the image by release tag in deployment and by digest where reproducible
   rollout matters; never deploy `latest`.
4. Add an OCI health check for the API. Treat broker connection and the retained
   `tariffkit/status` topic as MQTT health; a process-only container check would
   report healthy while disconnected.
5. Publish a multi-architecture image (`linux/amd64`, `linux/arm64`) with an
   SBOM and provenance attestation from the release workflow.

The API and publisher should remain separate containers. Combining them under a
process supervisor would couple failures, logs, health, and deployment cadence
without sharing meaningful runtime state.
