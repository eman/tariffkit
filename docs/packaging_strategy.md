# Architecture decision: packaging and repository strategy

- **Status:** Accepted
- **Date:** 2026-08-15
- **Decision owners:** Project maintainers

## Context

The project combines a dependency-free pricing and billing library, optional
network and service integrations, generated rate data, a Home Assistant custom
component, rate-data generation tools, and an account-specific audit harness.
All of these parts are maintained together and changes to one frequently need
validation across the others.

The original strategy proposed publishing separate core, client, and server
packages. That proposal correctly valued a lightweight runtime and clear domain
boundaries, but package boundaries did not match the code:

- optional dependencies are already isolated behind extras and lazy imports, so
  a default install has no third-party dependencies;
- `interop/` is pure runtime conversion rather than an I/O client;
- `export/` prices exported energy, while InfluxDB and PG&E access live under
  `sources/`;
- reading a PG&E statement and reconciling it against tracked account history
  is itself dependency-light, generic logic with no maintainer-specific
  content — only the mapping from *this project's own* real statements to
  computed-bill line items, and the orchestration that runs reconciliation
  across one account's whole history, are unpublished-harness-specific; and
- billing depends on the engine, configuration, tariffs, data, and time helpers,
  making a narrower “core” unsuitable for its actual consumers.

The current artifacts also violate one intended boundary: `regen/` is below the
packaged source tree and therefore ships in both wheel and sdist. The Home
Assistant build script vendors that whole tree and can copy ignored source
caches into its release.

## Decision drivers

- Keep the default installation dependency-free.
- Ship runtime code and static rate data together so they cannot drift.
- Validate engine, data, integrations, and billing atomically.
- Keep the Home Assistant integration small and conventionally packaged.
- Exclude account-specific and maintainer-only programs from distributions.
- Minimize release choreography while one team owns every surface.
- Use current Python packaging metadata and secure publication practices.

## Considered options

### One distribution and one repository

Feature dependencies remain extras. Runtime modules share one version and static
data release. Maintainer tools stay in the repository but outside artifacts.

### Multiple distributions in one repository

A core, client, and service package would isolate source archives, but introduce
cross-package constraints and coordinated releases without reducing the default
dependency set. The current dependency graph would also force arbitrary splits.

### Multiple repositories

Independent repositories provide ownership and access isolation. Neither exists
here today; splitting would instead duplicate CI and make cross-surface changes
non-atomic.

## Decision

Use **one repository and one public Python distribution**.

The distribution contains:

- pricing models, configuration, time handling, tariffs, export rates, and
  static data;
- billing, netting, ledgers, and true-up behavior;
- pure interoperability adapters;
- source adapters;
- named account profiles and their local persistence (`account/`), and the
  generic PG&E statement importer and reconciler that populates them
  (`providers/pge/`), each gated behind its own extra;
- the CLI, MQTT publisher, and web application.

The default install remains dependency-free. MQTT, web, portal, Home Assistant
source, InfluxDB, statement, and secrets capabilities use named extras and
lazy imports.

Rate-data generation moves to a repository-only tool namespace. What remains
repository-only in the audit harness is narrower than "statement parsing" —
it is the parts genuinely specific to reconciling *this project's own* real
statements against computed bills: the line-to-component mapping, attribution
rules, run orchestration, and portal-protocol research. The generic statement
importer and reconciler moved out of it into `providers/pge/` and ship
publicly; the harness now consumes that published code rather than owning it.
Neither the harness nor the rate-data tools ship in wheel or sdist, but both
retain strict typing, linting, tests, and CI coverage.

The Home Assistant integration declares an exact requirement on the published
distribution instead of vendoring it.

The project is renamed **TariffKit** before its first public release:

| Surface | Value |
|---|---|
| Product and repository | TariffKit / `tariffkit` |
| PyPI distribution | `tariffkit` |
| Python import | `tariffkit` |
| CLI | `tariffkit` |
| Environment prefix | `TARIFFKIT_` |
| Configuration directory | `~/.config/tariffkit/` |
| Home Assistant domain | `tariffkit` |

The name is globally neutral; documentation must still state that the initial
vendored providers and tariffs are PG&E/California-specific. A 2026-08-15
screen found no PyPI project or exact-name GitHub repository, but availability
is not trademark clearance.

## Consequences

### Benefits

- Core users install no service dependencies.
- One version identifies compatible code and data across every runtime surface.
- Home Assistant uses normal dependency management and no copied source tree.
- Maintainer and account-specific code cannot leak into public artifacts.
- Cross-cutting changes remain atomic and use one audit trail.

### Costs

- The wheel includes optional modules a core-only consumer does not import.
- Release versions remain coordinated across runtime surfaces.
- Repository CI covers more than the published package.

These costs are smaller than maintaining independent compatibility contracts at
the current project size.

## Future split triggers

Reconsider packages or repositories only when at least one boundary gains:

- independent maintainers or access controls;
- an independent release cadence or support policy;
- conflicting Python or dependency requirements;
- a stable public protocol that removes atomic source changes; or
- Home Assistant core inclusion requiring its own upstream workflow.

If a second distribution becomes justified, use a uv workspace with one
`pyproject.toml` per member and a shared lockfile. A workspace is unnecessary
while only one distribution exists.

## Packaging baseline

- Python 3.14 remains the declared and tested floor.
- Standardized `[project]` metadata is used (PEP 621).
- The MIT license uses an SPDX expression and declared license files (PEP 639).
- Runtime features use optional dependencies; development tools use dependency
  groups (PEP 735).
- `py.typed` remains in the wheel (PEP 561).
- uv manages Python, environments, locking, builds, and CI. `uv.lock` remains
  the repository lock; a PEP 751 `pylock.toml` is only needed for a consumer
  that requires the interchange format.
- Hatchling remains the backend unless another backend demonstrably simplifies
  the required package-data and exclusion rules.
- Release artifacts are built once, inspected, installed in a clean
  environment, and published with PyPI Trusted Publishing and PEP 740
  attestations.
- A reviewed release commit synchronizes the Python and Home Assistant
  versions. The manual workflow stages the same artifacts on TestPyPI, waits
  for protected PyPI approval, and publishes an immutable GitHub release only
  after PyPI succeeds. See the [release runbook](releases.md).

## References

- [pyproject.toml specification](https://packaging.python.org/en/latest/specifications/pyproject-toml/)
- [Dependency Groups](https://packaging.python.org/en/latest/specifications/dependency-groups/)
- [Core metadata](https://packaging.python.org/en/latest/specifications/core-metadata/)
- [pylock.toml specification](https://packaging.python.org/en/latest/specifications/pylock-toml/)
- [uv package guide](https://docs.astral.sh/uv/guides/package/)
- [uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/)
- [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
- [PyPI attestations](https://docs.pypi.org/attestations/)
- [Home Assistant integration manifest](https://developers.home-assistant.io/docs/creating_integration_manifest/)
- [HACS integration publishing](https://hacs.xyz/docs/publish/integration/)
