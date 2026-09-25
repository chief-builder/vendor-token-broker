# Security and MCP alignment

**Reviewed 2026-09-25 · software 1.1.0 (unreleased) · MCP 2026-07-28.**

The broker supplies credential custody and vendor OAuth flows inside a larger MCP deployment. Passing broker tests is not a claim that the complete deployment conforms to MCP. [The current specification](https://modelcontextprotocol.io/specification/2026-07-28) defines the protocol baseline.

## Control ownership and evidence

“Implemented” means present in this broker; “partial” identifies limits; “external” requires another component; “planned” is not available here. Source/test paths below refer to the private repository available to maintainers.

| Control | Status / owner | Implementation and evidence |
|---|---|---|
| PKCE S256, nonce, subject/browser binding | Implemented / broker | `main.py`, `hub_login.py`; `tests/unit/test_consent_binding.py`, `test_hub_login.py`, `tests/integration/test_consent.py` |
| Vendor callback issuer validation | Partial / broker | `main.py` callback checks; `tests/integration/test_consent.py`; metadata limitations below |
| Hub JWT revalidation | Implemented / broker | `hub.py`; `tests/unit/test_hub.py`, `tests/integration/test_security.py` |
| Scope ceiling and scope union | Implemented / broker policy | `main.py`, `refresh.py`; `tests/unit/test_scope_math.py`, `tests/integration/test_refresh.py` |
| Per-user custody, generation CAS, single-flight | Implemented / broker + custody + coordination | `custody.py`, `refresh.py`, `coordination.py`; storage, refresh, and multi-replica tests |
| Revocation | Implemented with exceptions / broker + vendor | `main.py`, `vendors.py`, `sweeper.py`; `tests/integration/test_grants.py` |
| No access-token issuance endpoint | Implemented / broker | `tests/unit/test_routes.py` audits seven routes; client assertions may still be signed |
| Sensitive values absent from tested logs | Implemented test coverage / broker | `tests/integration/test_security.py` checks sampled hub/vendor tokens, client secret, and assertion key; not an exhaustive proof for arbitrary provider errors |
| Protected-resource metadata and MCP discovery | External / MCP server and client | No MCP endpoint implementation here |
| Resource indicators and resource-specific token audience | External / MCP client, server, AS | Internal tier-audience checks do not establish MCP resource binding |
| MCP authorization challenges and registration | External / MCP stack | Broker status codes are an internal contract |
| Vendor consent via URL elicitation | Planned adapter / MCP server | [Integration design](mcp-integration.md); not exercised by broker tests |
| Token stripping and token-free MCP results | External / gateway and MCP server | Must be tested in the deployment; resolve deliberately returns a token to its trusted caller |
| Issuer-bound vendor client credentials | Partial / configuration control | Custody keys use vendor ID; issuer changes do not automatically invalidate credentials |
| Enterprise-Managed Authorization / ID-JAG | Planned / cooperating identity systems | `ema_status` tracks readiness; no exchange or automated drain implementation |

Protected-resource metadata, resource indicators, and intended-audience validation belong at the MCP boundary. [MCP authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)

## Known limitations

### Issuer discovery and callback checks

Vendor discovery currently checks endpoint shapes, but does not require an issuer or pin it against a separately trusted expected issuer. If the stored issuer is absent, the vendor callback skips issuer comparison. Explicit registry endpoints cannot currently express issuer/support fields in the schema.

The hub callback rejects a supplied issuer mismatch but does not reject an omitted issuer based on metadata advertisement. Vendor credentials are stored per vendor ID, without an issuer stamp that blocks reuse after a metadata change. Restrict registry/metadata administration and review provider changes. These are follow-up implementation items, not gaps fixed by these documentation changes.

The standards alignment is therefore partial: current MCP's callback checks depend on authenticated, validated issuer metadata. [Authorization response validation](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#authorization-response-validation)

### Scope and lifetime

The broker can cap requested and recorded scope names; it cannot narrow a vendor-issued token by editing metadata. An empty ceiling delegates permission policy to the vendor. The minimum token lifetime is a target with clamping and short-lived-token exceptions. See [scope and lifetime semantics](api.md#resolve-a-token).

### Revocation, historical storage, and recovery

Unsupported vendor revocation removes the local connection only. If custody fails after code redemption, revocation of the newly acquired grant is best-effort. `max_versions=2` retains up to two credential versions; scrubbing the current STALE entry does not erase every retained historical version.

CAS prevents a losing refresh write from overwriting a newer stored version. It cannot undo a refresh already consumed at the vendor. A replica failure at that point can burn a rotating-token family and require re-consent. Cache invalidation is best-effort, bounded by `CACHE_TTL_S` (default 60 seconds).

### Deployment controls

Use private ingress and gateway workload authentication, TLS, controlled custody policies, and audit logging configured in the custody backend. Those controls are not supplied by the development Compose stack. The broker can sign `private_key_jwt` client-authentication assertions, but does not issue access tokens. Treat vendor client signing keys as credential material.

Route `broker.stale.mass` to monitoring if an on-call notification is required: emitting `page: true` does not itself contact an alert service. Availability and latency values in the design are targets, not measured guarantees.

## Verification record

Review baseline: application commit `99bd39f`. Checks on 2026-09-25:

| Check | Result |
|---|---|
| Unit suite | 321 passed |
| Docker integration / memory | 61 passed |
| Docker integration / Redis | 61 passed |
| Multi-replica (local, isolated) | 7 passed |
| Multi-replica (CI) | First run: 20-parallel single-flight test saw 2 refreshes; rerun of the failed job passed |
| Real GitHub | Skipped; App credentials not configured |

The CI failure is a test-timing race, not a single-flight defect: mock tokens (60 seconds) sit inside `REFRESH_BUFFER_S` (300), so a resolve whose first read lands after the first refresh completes legitimately refreshes again.

At the earlier `2317b07` review, the first multi-replica failure was consistent with a retained 120-second sweep lease from the preceding standalone Redis broker exceeding the test's 60-second deadline. Its remaining TTL was not captured at failure, so this explanation is not definitive. Host/container clocks aligned. Documented [test isolation](quickstart.md#run-the-automated-checks) avoids that handoff ambiguity.

These checks use the mock vendor and do not establish real-provider interoperability, MCP client compatibility, production load capacity, or penetration-test results. [Manual verification](smoke-tests.md) explains the individual probes.

## Enterprise migration

EMA is optional and requires compatible clients, IdPs, and resource authorization servers. Evaluate downstream vendor access before retiring a broker integration. Keep readiness evidence per vendor, with a review date. An `ema_status` change alone has no runtime effect. [Enterprise-Managed Authorization](https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization)
