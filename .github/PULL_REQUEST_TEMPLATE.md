## Summary

<!-- What changed, and why does it matter to users or maintainers? -->

## Validation

<!-- List the exact checks run and their results. -->

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest --cov=tariffkit --cov-report=term-missing`
- [ ] `uv run pytest audit/tests`
- [ ] Not applicable checks are explained below.

## Contributor checklist

- [ ] The change is focused and includes tests where behavior changed.
- [ ] User-facing behavior is documented.
- [ ] `CHANGELOG.md` is updated under `Unreleased`.
- [ ] Generated files were regenerated from their published sources, not hand-edited.
- [ ] The audit harness remains repository-only and outside distribution artifacts.
- [ ] I included no secrets, `.env` contents, bills/statements, account identifiers, diagnostics, exports, or raw portal responses.
- [ ] This pull request does not disclose a vulnerability; security reports use [private vulnerability reporting](https://github.com/eman/tariffkit/security/advisories/new).

## Additional notes

<!-- Explain skipped checks, compatibility considerations, or public source provenance. -->

