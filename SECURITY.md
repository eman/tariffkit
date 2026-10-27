# Security policy

## Supported versions

TariffKit is pre-release. Security fixes are applied to the latest release and
the `main` branch only.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or include credentials,
utility statements, account identifiers, or Home Assistant diagnostics in one.

Use [GitHub's private vulnerability reporting form][report] from the
repository's **Security** tab. Include the affected version, impact,
reproduction steps, and a minimal sanitized example. You should receive an
acknowledgement within seven days. If that form is unavailable, contact the
maintainer through their GitHub profile to request a private channel before
sharing details.

Rotate any credential that may have been disclosed. TariffKit stores secrets in
the operating-system keyring, but reports can still accidentally include values
copied from environment variables or external service responses.

## Dependency auditing

CI audits the locked production extras separately from the complete development
graph, including the Home Assistant test stack. Both reports retain every
finding rather than hiding policy exceptions.

Both audits must come back clean. An advisory that cannot be fixed yet -- one
pinned by an upstream dependency, say -- may be accepted temporarily by adding
`.github/dependency-audit-policy.json`, naming the package, version and exact
advisories, why they are not reachable from TariffKit, and an expiry date. An
`audits` list limits it to `production` or `development`; without one it
covers both. CI then requires each covered audit to report exactly those
package, version and advisory tuples, and every other audit to come back
clean, so a missing or additional finding fails, and the policy expires
automatically, forcing review even if upstream has not released fixed pins.

One is in force, for the development audit only: `pillow==12.3.0`, which
Home Assistant pins exactly through 2026.10.4 and which reaches TariffKit only
through the Home Assistant test stack. It expires on 2026-12-01.

[report]: https://github.com/eman/tariffkit/security/advisories/new
