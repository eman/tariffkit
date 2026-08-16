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

Home Assistant 2026.8.2 exactly pins `cryptography==48.0.1`, and the matching
pytest plugin is currently the newest compatible release. On Linux, the
production `secrets` extra also installs that version through
`keyring -> SecretStorage -> cryptography`. The three advisories are not
reachable from TariffKit: it uses keyring's high-level password storage API and
does not import cryptography or call the affected PKCS#7 decryption and X.509
verification APIs. The temporary exception is declared in
[`.github/dependency-audit-policy.json`](.github/dependency-audit-policy.json).
CI independently requires both audits to contain exactly those package,
version, and advisory tuples; a missing or additional finding fails. The policy
also expires automatically, forcing review even if upstream has not released
fixed pins.

[report]: https://github.com/eman/tariffkit/security/advisories/new
