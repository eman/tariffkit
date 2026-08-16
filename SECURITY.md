# Security policy

## Supported versions

TariffKit is pre-release. Security fixes are applied to the latest release and
the `main` branch only.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or include credentials,
utility statements, account identifiers, or Home Assistant diagnostics in one.

Once the repository is public, use GitHub's private vulnerability reporting
from its **Security** tab. Include the affected version, impact, reproduction
steps, and a minimal sanitized example. You should receive an acknowledgement
within seven days. If that form is unavailable, contact the maintainer through
their GitHub profile to request a private channel before sharing details.

Rotate any credential that may have been disclosed. TariffKit stores secrets in
the operating-system keyring, but reports can still accidentally include values
copied from environment variables or external service responses.
