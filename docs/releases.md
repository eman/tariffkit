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
immutability. GitHub environments named `testpypi` and `pypi` must exist.
Restrict `testpypi` to `main`. Restrict `pypi` to the `main` branch and the
`v*` tag pattern, because publishing runs from the release tag rather than
from a branch. Neither environment requires a reviewer: pushing the tag is the
release decision.
Protect `main` with the CI and security checks required for merge. Restrict
tag creation to maintainers, because a `v*` tag publishes to PyPI without a
further prompt.

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
VERSION=0.4.1
uv run python -m tools.release prepare "$VERSION"
git diff
```

The preparation command updates the project and lockfile versions, the Home
Assistant manifest and exact requirement, versioned documentation, changelog,
and comparison links. It validates release identity before it returns, and the
release workflow revalidates identity and version availability, so no separate
local check is required.

Review every change, then open a release PR. The PR must pass normal CI, HACS
validation, and hassfest. Merge it without creating a tag.

For a release candidate, use a version such as `0.4.1rc1` and follow the same
process. A later candidate or stable release gets a new version; published
candidate files are never replaced.

## Publish

Push the release tag at the merged commit on `main`:

```bash
VERSION=0.4.1
git checkout main
git pull
git tag "v$VERSION"
git push origin "v$VERSION"
```

The tag starts the Release workflow, which verifies that the tagged commit is
on `main`, that the tag matches the committed version, and that PyPI has no
such version. It then runs the tests, artifact-boundary checks, clean
installation, sdist reconstruction, and deterministic HACS ZIP construction,
uploads the wheel and sdist to PyPI with attestations, and publishes the GitHub
release with the wheel, sdist, `tariffkit.zip`, and `SHA256SUMS`.

Nothing is published unless every check passes, and there is no approval
prompt. Pushing the tag is the release decision, so push it only from a
reviewed release commit already merged to `main`.

## Rehearse without publishing

Rehearsal is optional for a routine release, because the tag-triggered run
performs the same validation before it publishes anything. Use it when the
release workflow itself changed, or to stage on TestPyPI.

From the repository's **Actions → Release → Run workflow** menu, select `main`
and leave the version blank to rehearse the committed version. The run
validates and builds without requesting a PyPI token, creating a tag, or
publishing. Download the run artifact to inspect the exact files.

To stage the rehearsed artifacts on TestPyPI, enable **Stage the rehearsed
artifacts on TestPyPI**, then install the staged command in an isolated uv
environment, using TestPyPI for TariffKit and PyPI for its dependencies:

```bash
VERSION=0.4.1
uvx --from "tariffkit==$VERSION" \
  --index https://test.pypi.org/simple \
  --default-index https://pypi.org/simple \
  tariffkit --version
```

TestPyPI staging is optional: account or verification problems there must not
block a production release. The HACS ZIP is never placed in the PyPI upload
directory; it travels unchanged in the same Actions artifact and is attached
only to GitHub.

Resolve any failure in a new PR. Do not bypass a release check.

## Verify a published release

Install from PyPI without using the checkout:

```bash
VERSION=0.4.1
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

**When the tagged run fails before publishing:** nothing reached PyPI and no
GitHub release exists. Fix the source or workflow in a new PR, delete the tag,
and push it again at the new commit on `main`.

```bash
git push origin ":refs/tags/v$VERSION"
git tag -d "v$VERSION"
```

**After TestPyPI staging but before a production tag:** prepare a new version;
do not overwrite the TestPyPI release.

**After any PyPI file is uploaded:** treat the version and uploaded files as
immutable. Retry only when the publisher explicitly supports completing a
partial upload with byte-identical remaining files. If artifact identity is
uncertain, publish a new patch version.

**For a harmful production release:** yank it from the PyPI release page with a
specific reason, mark its changelog heading `[YANKED]`, and publish a corrective
version. Use a GitHub security advisory when the defect is security-sensitive.
Never delete and republish a production version.
