# Releasing TariffKit

This runbook is for maintainers publishing the Python distribution and the
matching Home Assistant integration version. TariffKit uses a reviewed release
commit, one set of distribution files, PyPI Trusted Publishing, and curated
release notes. The same build also produces the HACS integration artifact
`tariffkit.zip`.

## Version and changelog policy

`pyproject.toml` is the canonical version source. The release command keeps it
in sync with `uv.lock` and
`custom_components/tariffkit/manifest.json`. The Home Assistant manifest uses
the same version and requires that exact TariffKit distribution.

Versions follow normalized [PEP 440](https://packaging.python.org/en/latest/specifications/version-specifiers/)
and [Semantic Versioning](https://semver.org/):

- use `MAJOR.MINOR.PATCH` for stable releases;
- use `MAJOR.MINOR.PATCHrcN` only for release candidates;
- before 1.0, increment the minor version for compatible features or material
  data additions and the patch version for compatible fixes;
- after 1.0, increment the major version for breaking changes; and
- never reuse a version published to PyPI or TestPyPI.

Maintain `CHANGELOG.md` as user-oriented prose under `Unreleased`. Use the
categories `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, and `Security`
in that order, omitting empty categories. Do not copy commit titles into the
changelog. Mark a yanked release as `[YANKED]` in its version heading.

## One-time publisher setup

PyPI and TestPyPI are separate services with separate accounts, projects, and
publisher registrations. Configuration on one does not configure the other.
TestPyPI staging is optional: account or verification problems there must not
block a production release that passes the complete artifact rehearsal and
protected PyPI approval.

If using TestPyPI, create an account at
<https://test.pypi.org/account/register/>. Verify its email address, enable
two-factor authentication under **Account settings**, and store the recovery
codes securely. Then sign in and open
<https://test.pypi.org/manage/account/publishing/>. Under **Add a new pending
publisher**, select **GitHub Actions** and enter:

| TestPyPI form label | Enter exactly |
|---|---|
| PyPI Project Name | `tariffkit` |
| Owner | `eman` |
| Repository name | `tariffkit` |
| Workflow name | `release.yml` |
| Environment name | `testpypi` |

Click **Add**. Confirm the resulting pending publisher shows all five values
exactly. In particular, the workflow field is only the filename `release.yml`,
not `.github/workflows/release.yml`, and the environment is lowercase
`testpypi`.

For production, create or sign in to the account at
<https://pypi.org/account/register/>. Verify its email, enable two-factor
authentication, and store its recovery codes. Open
<https://pypi.org/manage/account/publishing/>. Under **Add a new pending
publisher**, select **GitHub Actions** and enter:

| PyPI form label | Enter exactly |
|---|---|
| PyPI Project Name | `tariffkit` |
| Owner | `eman` |
| Repository name | `tariffkit` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

Click **Add** and verify the displayed registration. The only intended
difference from TestPyPI is the environment name: production uses `pypi`.
In the GitHub repository's **Settings → General → Releases**, enable release
immutability. GitHub environments named `testpypi` and `pypi` must exist;
restrict both to `main` and require approval on `pypi`.
Protect `main` with the CI and security checks required for merge. Restrict
tag creation to maintainers and release automation where repository rulesets
support it.

Do not create PyPI API-token secrets. The workflow receives short-lived OIDC
credentials from the protected environments. Do not manually create the
`tariffkit` project or upload a placeholder distribution: the first successful
Trusted Publishing upload creates the project and converts the pending
publisher into a normal publisher. Create pending publishers shortly before the
first publication because they do not reserve a project name.

## Prepare a release

Start from an up-to-date branch based on `main`. Curate `Unreleased`, choose the
next version, and run:

```bash
VERSION=0.2.3
uv run python -m tools.release prepare "$VERSION"
uv run python -m tools.release check --version "$VERSION" --tag "v$VERSION"
uv run python -m tools.release available "$VERSION" --repository pypi
git diff --check
git diff
```

If TestPyPI staging is planned, also check it before opening the release PR:

```bash
uv run python -m tools.release available "$VERSION" --repository testpypi
```

The preparation command updates the project and lockfile versions, the Home
Assistant manifest and exact requirement, versioned documentation, changelog,
and comparison links. Review every change, then open a release PR. The PR must
pass normal CI, HACS validation, and hassfest. Merge it without creating a tag
or GitHub release.

For a release candidate, use a version such as `0.2.3rc1` and follow the same
process. A later candidate or stable release gets a new version; published
candidate files are never replaced.

## Rehearse without publishing

From the repository's **Actions → Release → Run workflow** menu, select `main`,
enter the exact prepared version, and select `dry-run`. The workflow performs
release identity checks, tests, artifact-boundary checks, clean installation,
sdist reconstruction, deterministic HACS ZIP construction, checksums, and
release-note extraction. It does not request an OIDC token, create a tag, or
publish anything. Download the run artifact and confirm `tariffkit.zip`
contains `manifest.json` and `__init__.py` at ZIP root, not beneath another
`custom_components` directory.

Resolve any failure in a new PR. Do not bypass a release check.

## Publish

1. Dispatch **Actions → Release → Run workflow** from `main` with the exact
   version and `release` mode. Enable **Stage the exact artifacts on TestPyPI**
   only when its account and pending publisher are ready.
2. If TestPyPI staging is enabled, wait for it and the draft GitHub release to
   complete. Install the staged command in an isolated uv environment, using
   TestPyPI for TariffKit and PyPI for its dependencies:

   ```bash
   VERSION=0.2.3
   uvx --from "tariffkit==$VERSION" \
     --index https://test.pypi.org/simple \
     --default-index https://pypi.org/simple \
     tariffkit --version
   ```

3. If TestPyPI staging is enabled, inspect its description, metadata, wheel,
   sdist, hashes, and attestations. In every release, inspect the draft GitHub
   release notes and confirm the wheel, sdist, and `tariffkit.zip` hashes match
   `SHA256SUMS`.
4. Approve the pending `pypi` environment deployment. The workflow uploads the
   already-tested files to PyPI, then publishes the prepared GitHub draft. Do
   not create or move the release tag manually.

When enabled, TestPyPI receives the exact Python files later sent to PyPI and
GitHub. The HACS ZIP is never placed in the PyPI upload directory; it travels
unchanged in the same Actions artifact and is attached only to GitHub.

## Verify a published release

Install from PyPI without using the checkout:

```bash
VERSION=0.2.3
uvx --refresh --from "tariffkit==$VERSION" tariffkit --version
uvx --refresh --from "tariffkit[all]==$VERSION" tariffkit info
gh release download "v$VERSION" --repo eman/tariffkit --dir release-assets
(cd release-assets && shasum -a 256 -c SHA256SUMS)
unzip -Z1 release-assets/tariffkit.zip
```

Confirm the PyPI files show attestations and verify a distribution against the
repository identity with
[`pypi-attestations`](https://docs.pypi.org/attestations/consuming-attestations/):

```bash
uvx pypi-attestations verify pypi \
  --repository https://github.com/eman/tariffkit \
  <PYPI_DISTRIBUTION_FILE_URL>
```

Finally, confirm that the released
`custom_components/tariffkit/manifest.json` has the same `version` and the
single requirement `tariffkit==<version>`. Add the repository to HACS as an
integration and install that release; Home Assistant must resolve the exact
PyPI dependency and complete the config flow.

## Recover from a failed release

**Before TestPyPI publication:** fix the source or workflow in a new PR and
rerun the same version only if neither package index contains it.

**After TestPyPI but before PyPI:** cancel production. Delete the draft GitHub
release and its tag only after confirming PyPI has no files for that version.
Prepare a new version; do not overwrite the TestPyPI release.

```bash
gh release delete "v$VERSION" --repo eman/tariffkit --yes --cleanup-tag
```

**After any PyPI file is uploaded:** treat the version and uploaded files as
immutable. Retry only when the publisher explicitly supports completing a
partial upload with byte-identical remaining files. If artifact identity is
uncertain, publish a new patch version.

**For a harmful production release:** yank it from the PyPI release page with a
specific reason, mark its changelog heading `[YANKED]`, and publish a corrective
version. Use a GitHub security advisory when the defect is security-sensitive.
Never delete and republish a production version.
