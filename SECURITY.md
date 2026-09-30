# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**[Report a vulnerability](https://github.com/chief-builder/vendor-token-broker/security/advisories/new)**
(Security tab → Advisories → Report a vulnerability). Do not open a public
issue, pull request, or discussion for a suspected vulnerability.

Include what you can: the affected component (broker or gateway), version or
commit, configuration, steps to reproduce, and impact. Never include real
tokens or secrets; the local test stack is enough to reproduce most issues.

You can expect an acknowledgement within 7 days. Fixes are released on
`main` and noted in [CHANGELOG.md](CHANGELOG.md); reporters are credited
unless they prefer not to be.

## Supported versions

| Version | Supported |
|---|---|
| 1.1.x (`main`) | Yes |
| 1.0.x | No |

## Scope notes

- Everything under `tests/stack/` (the Keycloak realm, compose defaults such
  as `root`, `mock-secret`, `hub-login-secret`, and the keypair in
  `tests/stack/keys/`) is test-only material for the local stack. It protects
  nothing, so its presence is not a vulnerability.
- Documented limitations are listed in
  [docs/security.md](docs/security.md#known-limitations). Reports that show
  one of them is worse than described are welcome.
