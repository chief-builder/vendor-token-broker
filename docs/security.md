# Security and MCP alignment

**Reviewed 2026-09-26 · software 1.1.0 (unreleased) · MCP 2026-07-28.**

This page lists which security controls the broker and gateway have, where the proof lives, and what is still missing.

- The broker stores each user's vendor tokens and runs the vendor sign-in (OAuth) flows. It is one part of a larger MCP deployment.
- The shipped [MCP gateway](mcp-gateway.md) handles the MCP boundary for GitHub's MCP server.
- Passing these tests does not mean a complete deployment meets the MCP standard. [The current specification](https://modelcontextprotocol.io/specification/2026-07-28) sets the protocol baseline.

## Control ownership and evidence

Status words in the table:

- **Implemented**: present in this broker (or the gateway, where named).
- **Partial**: present, with limits the row names.
- **External**: another component must provide it.
- **Planned**: not available here.

Source and test paths refer to the private repository available to maintainers. The hub is your company's sign-in service (identity provider).

| Control | Status / owner | Implementation and evidence |
|---|---|---|
| PKCE S256, nonce, subject/browser binding | Implemented / broker | `main.py`, `hub_login.py`; `tests/unit/test_consent_binding.py`, `test_hub_login.py`, `tests/integration/test_consent.py` |
| Vendor callback issuer validation | Partial / broker | `main.py` callback checks; `tests/integration/test_consent.py`; see the metadata limits below |
| Hub JWT revalidation | Implemented / broker | `hub.py`; `tests/unit/test_hub.py`, `tests/integration/test_security.py` |
| Scope ceiling and scope union | Implemented / broker policy | `main.py`, `refresh.py`; `tests/unit/test_scope_math.py`, `tests/integration/test_refresh.py` |
| Per-user custody, generation CAS, single-flight | Implemented / broker + custody + coordination | `custody.py`, `refresh.py`, `coordination.py`; storage, refresh, and multi-replica tests |
| Revocation | Implemented with exceptions / broker + vendor | `main.py`, `vendors.py`, `sweeper.py`; `tests/integration/test_grants.py` |
| No access-token issuance endpoint | Implemented / broker | `tests/unit/test_routes.py` checks there are exactly seven routes. The broker may still sign client assertions |
| Sensitive values absent from tested logs | Implemented test coverage / broker | `tests/integration/test_security.py` checks sample hub/vendor tokens, the client secret, and the assertion key. It does not prove every possible provider error is clean |
| Protected-resource metadata and MCP discovery | Implemented / gateway | `mcp_gateway/server.py` (`build_auth`); `tests/unit/test_gateway_auth.py`, `tests/integration/test_mcp_gateway.py`; checked with Claude Code 2.1.283 |
| Resource-specific token audience | Partial / gateway + hub | The gateway requires `aud` = its resource URL, a pinned algorithm, issuer, and scope (tested). Keycloak ignores the RFC 8707 `resource` parameter, so the audience comes from the required `mcp-gateway` scope |
| MCP authorization challenges | Implemented / gateway | 401 `WWW-Authenticate` with `resource_metadata` and `scope`. Every refusal is audited as `gateway.auth` with its reason |
| MCP client registration | External / hub | The development realm allows dynamic registration from localhost only, with consent (tested). Production policy belongs to the IdP |
| Identity handoff without token passthrough | Implemented / gateway + hub | RFC 8693 token exchange. The broker rejects a raw MCP token (`tests/integration/test_keycloak_hub.py`) |
| Vendor consent via URL elicitation | Implemented / gateway | Both protocol eras and the fallback for clients without the capability; `tests/unit/test_gateway.py`, `tests/integration/test_mcp_gateway.py`; checked with Claude Code 2.1.283 |
| Token-free MCP results and logs | Implemented for the shipped gateway | The leak test greps every container for the MCP token, hub JWT, and GitHub token. Custom gateways must run their own test. Resolve returns a token to its trusted caller on purpose |
| Read-only tool allowlist | Implemented / gateway | Allowlist, plus `X-MCP-Readonly` and `X-MCP-Lockdown` on every upstream call (tested against the stand-in) |
| Issuer-bound vendor client credentials | Partial / configuration control | Storage keys use the vendor ID. A change of issuer does not invalidate stored credentials on its own |
| Enterprise-Managed Authorization / ID-JAG | Planned / cooperating identity systems | `ema_status` tracks readiness. There is no exchange and no automated drain |

Protected-resource metadata, resource indicators, and checking the intended audience belong at the MCP boundary. [MCP authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)

## Known limitations

### Issuer discovery and callback checks

The broker does not fully check who issued a vendor's sign-in response (the "issuer"). In detail:

- Vendor discovery checks the shape of the endpoints. It does not require an issuer, or compare it with a separately trusted expected issuer.
- If no issuer is stored, the vendor callback skips the issuer comparison.
- The registry schema has no way to state issuer or support fields for explicit endpoints.
- The hub callback rejects an issuer that does not match. It does not reject a missing issuer when the metadata says one should be sent.
- Vendor credentials are stored per vendor ID, with no issuer stamp. Nothing blocks their reuse after a metadata change.

**What to do:** restrict who can change the registry and metadata, and review provider changes. These are follow-up code items. Documentation changes do not fix them.

So standards alignment here is partial. Current MCP callback checks depend on issuer metadata that is authenticated and checked. [Authorization response validation](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#authorization-response-validation)

### Scope and lifetime

- The broker can cap the scope names it requests and records. It cannot narrow a token the vendor issued by editing metadata.
- An empty ceiling leaves permission policy to the vendor.
- The minimum token lifetime is a target. It has clamping and exceptions for short-lived tokens.

See [scope and lifetime semantics](api.md#resolve-a-token).

### Revocation, historical storage, and recovery

- If the vendor does not support revocation, disconnecting removes only the local connection.
- If storage fails after the broker redeems a code, revoking the new grant is best-effort.
- `max_versions=2` keeps up to two credential versions. Blanking the current STALE entry does not erase every older kept version.
- CAS (the storage write check) stops a losing refresh write from overwriting a newer stored version. It cannot undo a refresh the vendor has already used. A replica failure at that point can burn a rotating-token family and force the user to re-consent.
- Cache invalidation is best-effort. Its limit is `CACHE_TTL_S` (default 60 seconds).

### MCP gateway

- It depends on FastMCP 4.0.10 and about 50 hash-pinned transitive packages.
- Its JWKS cache can keep trusting a removed hub signing key for up to an hour.
- It cannot send tool-list changes to 2026-07-28 clients (no `subscriptions/listen`). The pinned tool list works around this.
- The local realm's self-registration policy is for development only.

Details: [gateway limitations](mcp-gateway.md#known-limitations).

### Deployment controls

The development Compose stack does not supply these controls. You must add them:

- Private ingress, and workload authentication for the gateway.
- TLS.
- Controlled storage policies.
- Audit logging set up in the storage backend.

The broker can sign `private_key_jwt` client-authentication assertions. It does not issue access tokens. Treat vendor client signing keys as credential material.

`broker.stale.mass` carries `page: true`, but that does not contact an alert service. Route the event to monitoring if you need an on-call notification. Availability and latency values in the design are targets, not measured guarantees.

## Verification record

Review baseline: application commit `a4c7a4b`. Checks on 2026-09-26:

| Check | Result |
|---|---|
| Unit suite | 375 passed |
| Docker integration / memory | 61 passed |
| Gateway profile (Keycloak hub, broker-kc, gateway, GitHub stand-in) | 29 passed |
| CI run 36267909800 (lint, unit, image builds, integration memory and Redis, gateway, multi-replica) | All jobs passed |
| Real GitHub through the gateway (`test_external_github_mcp.py`) | 2 passed with a configured GitHub App |
| Claude Code 2.1.283 against the gateway and GitHub's MCP server | Sign-in with dynamic registration, in-client GitHub connection prompt, and `get_me` all worked |

### Earlier flaky runs

Earlier CI runs at `ba03ed4` and `2607389` each failed one 20-parallel single-flight test once. Rerunning the failed job passed.

This is a timing race in the test, not a single-flight bug. Mock tokens last 60 seconds, which is inside `REFRESH_BUFFER_S` (300). So a resolve whose first read lands after the first refresh finishes correctly refreshes again.

At the earlier `2317b07` review, the first multi-replica failure had a different likely cause: the standalone Redis broker that ran before it kept its 120-second sweep lease, longer than the test's 60-second deadline. The lease's remaining TTL was not captured at failure, so this is not certain. Host and container clocks matched. The documented [test isolation](quickstart.md#run-the-automated-checks) avoids this handoff problem.

### What these checks do not show

- Most checks use the mock vendor and the GitHub stand-in.
- The real-GitHub and Claude Code checks show that this one configuration works together. They say nothing wider.
- None of this shows production load capacity or penetration-test results.

[Manual verification](smoke-tests.md) explains the individual probes.

## Enterprise migration

Enterprise-Managed Authorization (EMA) is optional. It needs compatible clients, IdPs, and resource authorization servers.

- Check downstream vendor access before you retire a broker integration.
- Keep readiness evidence per vendor, with a review date.
- Changing `ema_status` alone has no runtime effect.

[Enterprise-Managed Authorization](https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization)
