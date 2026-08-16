# Contributing to TariffKit

Thank you for improving TariffKit. Contributions may be code, tests,
documentation, rate-data source updates, or reproducible bug reports.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
For usage questions, see [Support](SUPPORT.md). Report vulnerabilities through
the private process in [Security](SECURITY.md), not in an issue or pull request.

## Protect private utility data

Everything in issues, pull requests, comments, and CI logs is public. Never
include:

- credentials, tokens, cookies, or `.env` contents;
- utility bills or statement PDFs;
- account, meter, service-agreement, or premise identifiers;
- Home Assistant diagnostics that have not been reviewed and sanitized; or
- raw portal responses, Green Button downloads, or other account exports.

Use a minimal synthetic example. If a secret was exposed, remove it from the
public report and rotate it immediately; deleting a comment does not erase its
history.

## Set up the development environment

TariffKit develops and tests against **Python 3.14.2** with
[uv](https://docs.astral.sh/uv/):

```bash
uv python install 3.14.2
uv sync --all-extras --group dev --python 3.14.2
```

Run commands through `uv run` so they use the locked environment.

## Make a focused change

1. Search existing issues and pull requests.
2. Create a branch from `main`.
3. Keep the change narrowly scoped and add tests for changed behavior.
4. Update user documentation for user-facing behavior.
5. Add a prose entry under `## [Unreleased]` in `CHANGELOG.md`.

Public APIs require timezone-aware datetimes and preserve the `locked`, `exact`,
and `complete` pricing flags. Do not hide domain failures, fabricate missing
pricing inputs, or merge fixed daily charges into marginal per-kWh prices.

### Generated rate data

Files under `src/tariffkit/data/` are generated from published source documents.
Do not hand-edit them. Follow [Maintaining rate data](docs/data.md), add a new
effective-dated vintage rather than rewriting history, and include provenance.
The export archive is large; tests marked `slowdata` require the full upstream
dataset and are not part of a routine local run.

### Audit harness boundary

Read [audit/README.md](audit/README.md) before changing `audit/`. The harness is
repository-only, account-specific, and intentionally excluded from wheel and
source distributions. Never add statements, credentials, `.env`, portal
responses, or account configuration to the repository. Do not move audit-only
logic into the public package merely to make it shippable, and do not collapse
its distinct disagreement (`1`) and unable-to-check (`2`) exit codes.

## Run the checks

Run the same strict checks as CI:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=tariffkit --cov-report=term-missing
uv run pytest audit/tests
uv build --no-sources
```

The main pytest configuration does not include `audit/tests`, so both pytest
commands are required. Use a targeted test while developing, then run the full
set before requesting review. Never run tests marked `live` for routine
validation; they sign in to the utility portal.

## Open a pull request

Complete the pull request template, explain the user-visible effect and the
validation performed, and link the relevant issue. Maintainers may ask for a
smaller change or additional source evidence, especially for pricing data.

