# Documentation site review — 2026-09-25

The published site is synchronized with this repository, but needs corrections before it can accurately explain its relationship to current MCP. Its strongest material is the broker lifecycle; its weakest areas are reader onboarding, integration boundaries, and evidence for broad security claims.

## Review scope and verification

Reviewed the application modules, configuration, registry/schema, deployment examples, documentation and generator, CI, and relevant unit/integration tests. Baseline: commit `2317b07`; package version `1.1.0`, still marked unreleased in CHANGELOG.

- The downloaded public site is byte-for-byte identical to `docs/index.html`.
- Rendering the Markdown through the existing generator produces identical HTML, checked without writing the generated file.
- All **319 unit tests pass** (five warnings).
- Chrome inspection: **20/20 Mermaid blocks render SVG**, no captured warning/error logs; **48 headings have no heading anchors**. Navigation provides only three document jumps and a repository link.
- Docker integration verification was subsequently completed; results and one initial multi-replica failure are recorded below. External GitHub was skipped because credentials were not configured. Load testing and mobile visual checks were not run.

### Docker integration verification — 2026-09-25

Built the current application into the acceptance-stack images and verified the running broker is version `1.1.0`. Ran the single-replica suites sequentially with the additional replicas stopped, and confirmed each selected backend inside the broker container.

| Suite | Result | Duration |
|---|---|---|
| Integration, memory coordination | 61 passed; 8 deselected | 49.25s |
| Integration, Redis coordination | 61 passed; 8 deselected | 49.64s |
| Multi-replica, initial run | 6 passed, 1 failed | 78.12s |
| Multi-replica, isolated rerun | 7 passed | 27.76s |
| Optional external GitHub | 1 skipped: `GITHUB_CLIENT_ID` unset | 0.02s |

The completed matrix has **129 passing integration executions** (61 scenarios under each backend plus seven multi-replica scenarios), in addition to the 319 unit tests. No application or test code changes were needed.

Commands used after starting each corresponding Compose profile:

```sh
.venv/bin/pytest tests/integration -m 'not external and not multi' -q
BROKER_URL=http://localhost:8400 BROKER_CONTAINERS=vtb-broker-a,vtb-broker-b \
  .venv/bin/pytest tests/integration/test_multi_replica.py -q -rs
.venv/bin/pytest tests/integration/test_external_github.py -q -rs
```

Coverage exercised against Docker includes real mock-provider OAuth exchanges, subject/browser consent binding, callback replay and issuer checks, vendor client assertions, scope expansion, rotating-token concurrency, cache expiry, custody migration and retention, revocation, dependency outages, token-leak checks, and the frozen API contract. Multi-replica tests exercise load-balanced concurrency, cross-replica consent/invalidation, abandoned-refresh takeover, sweep leadership, Redis outage behavior, and paused-replica recovery.

**Test reliability finding:** `test_single_sweep_leader_retries_revoke_once` initially saw no sweep-retry audit event within its 60-second deadline. The preceding standalone Redis broker uses a 60-second sweep interval and a lease of twice that interval (120 seconds); the multi-replica test stops that broker but does not release or wait out its lease. This is consistent with a retained lease delaying takeover, although the old lease's remaining TTL was not captured at the instant of failure. Host/container clocks were checked and aligned. After isolating the replicas and allowing the handoff, the entire seven-test suite passed, including the sweep test. Preserve this initial failure in the record.

Recommended follow-up: isolate coordination state between test profiles or explicitly wait for the previous leader's lease to expire before measuring takeover. Document sweep failover latency as dependent on the previous leader's configured lease. These tests confirm the covered broker behaviors; they do not establish full MCP interoperability or real-vendor compatibility.

JUnit evidence is saved locally under `/tmp/vtb-integration-memory.xml`, `/tmp/vtb-integration-redis.xml`, `/tmp/vtb-integration-multi.xml`, `/tmp/vtb-integration-multi-rerun.xml`, and `/tmp/vtb-integration-external.xml`.

Current MCP baseline: **2026-07-28**, confirmed by the official [latest specification redirect](https://modelcontextprotocol.io/specification/latest). Record the exact revision and review date on the site.

## Priority 1 — correct the MCP integration story

### Separate the three authorization relationships

The architecture in `docs/design.md:39` omits an explicit MCP server and puts a hub JWT directly on the tool-call arrow. `docs/operations.md:107` describes mapping missing vendor consent and scope expansion to client-facing 401 challenges. `docs/smoke-tests.md:121` calls this an MCP authorization-required challenge. The repository contains the broker, not the gateway plugin or an MCP protocol implementation.

Add one introductory diagram with named boundaries:

1. MCP client → MCP server: authorization to call the MCP service.
2. Trusted server/gateway → broker: the project's internal resolve API and hub-token contract.
3. Broker → vendor authorization server/API: the user's separate vendor grant.

Identify where the MCP server resides in the intended deployment. Label token audience, validating component, and token recipient on each leg. Explain that `mcp_contract`, `mcp://tier/internal`, the algorithm allowlist, and broker problem titles are project conventions. A shared tier audience alone does not demonstrate MCP resource-specific audience enforcement.

The current MCP authorization specification requires protected-resource discovery, resource indicators, and intended-audience validation at the MCP boundary. Publish an ownership matrix for these requirements; their absence from this internal broker is not automatically a broker defect. [MCP authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)

### Distinguish vendor connection from MCP authentication

Preserve the broker's internal 404/409 wire contract. Label the documented Kong mapping as a legacy/custom adapter contract, not proof of MCP interoperability.

For a client already authorized to the MCP server, document a proposed URL-mode elicitation adapter for missing downstream vendor consent. Current MCP uses URL mode for third-party authorization, keeps the MCP bearer token unchanged, and carries elicitation through an `InputRequiredResult`. Only send URL elicitation when the client advertises support. Define cancellation, unsupported-client behavior, and bounded retries. Verify the grant at the broker after the browser flow; a user's acceptance alone is not proof of successful authorization. This adapter is **not implemented here**. [MCP elicitation](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation)

### Add a requirement → owner → implementation → evidence matrix

Use explicit states such as **implemented**, **partial**, **external responsibility**, and **planned**.

| Topic | Honest status today | Evidence / next documentation action |
|---|---|---|
| PKCE, browser and subject binding | Implemented for broker consent | `main.py`, `hub_login.py`, `test_consent_binding.py`, `test_hub_login.py`; explain the user journey |
| Vendor callback issuer checks | Partial end-to-end assurance | Callback comparison exists; metadata trust limitations below remain |
| Scope ceiling and scope union | Implemented internal policy | `consent_scopes`, `reconsent_scopes`, scope tests; distinguish recorded scopes from actual token permissions |
| Credential custody, refresh, revocation | Implemented with documented exceptions | Custody/refresh/lifecycle tests; publish limitations alongside guarantees |
| MCP resource metadata, resource indicators, audience validation, HTTP challenges | External MCP client/server/AS responsibility | No implementation or end-to-end evidence in this repository |
| Vendor token hidden from MCP client; hub token stripped upstream | Intended deployment boundary | Broker returns a token to its caller; gateway behavior and private ingress need separate evidence |
| URL-mode consent adapter and MCP wire lifecycle | Not implemented here | Add versioned integration example and adapter tests in the owning project |
| Enterprise-Managed Authorization / ID-JAG | Planned migration | `ema_status` is tracking metadata, not an exchange implementation or automatic drain switch |

Current MCP also changes protocol lifecycle and registration behavior. A short version note should assign per-request capabilities, MRTR, and routing headers to the MCP implementation, and mention DCR deprecation in favor of CIMD. These do not require turning the broker into an MCP endpoint. [2026-07-28 release](https://blog.modelcontextprotocol.io/posts/2026-07-28/)

EMA is an optional extension requiring cooperating clients, IdPs, and resource authorization servers. Present it as a conditional migration path. It does not establish that every downstream SaaS credential will become unnecessary. Replace the undated “most SaaS vendors” assertion with dated, verified per-vendor support information. [Enterprise-Managed Authorization](https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization)

### Track implementation limitations separately from copy changes

- `vendors.py:81` validates endpoint strings but does not require an issuer or compare it with a separately trusted expected issuer. `main.py:594` skips issuer comparison when the recorded issuer is absent. The registry's explicit-endpoint schema cannot express issuer or issuer-support metadata. Do not claim complete mix-up protection across all vendor configurations.
- `main.py:492` checks a supplied hub callback issuer, but does not reject an omitted issuer when hub metadata advertises support. Document the difference between hub and vendor legs, then close it with focused tests.
- Vendor credentials are keyed by vendor ID; no stored issuer binding prevents their reuse after metadata/issuer changes. Document the controlled configuration assumption and investigate explicit issuer binding before claiming current MCP client credential isolation.

These findings constrain a broad standards claim; they do not imply this broker must implement every MCP client requirement. Current MCP's issuer validation depends on validated metadata and checks callback issuer before code redemption. [MCP authorization response validation](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#authorization-response-validation)

## Priority 1 — fix inaccurate and stale statements

| Location | Finding | Recommended correction |
|---|---|---|
| `README.md:7`, `design.md:316`, `smoke-tests.md:373` | “No signing keys” conflicts with `client_auth.py:45`, which signs `private_key_jwt` using a custody key | “Does not issue access tokens; may sign vendor client-authentication assertions.” Route tests prove endpoint absence, not key absence |
| `README.md:16` | Guaranteed minimum TTL conflicts with clamping and short-lived-token exceptions | State the target and exceptions together; callers inspect `expires_at` |
| `design.md:66` | Hub arrow says “JWKS only” | Include OIDC discovery, browser login, and code redemption introduced in v1.1 |
| `design.md:3` | Version 1.0 appears above v1.1 behavior | Separate documentation revision, covered software version/release status, and MCP revision |
| `token-lifecycle.md:275`, ADR line 27 | Lock TTL is 15 seconds | Current default is 20 seconds; describe configuration constraints |
| `smoke-tests.md:126` | Authorize link described as 10-minute TTL | Link start window is 5 minutes; consent state defaults to 10 minutes |
| `smoke-tests.md:374` | Route test described as OpenAPI-level | It audits `app.routes`; OpenAPI endpoints are disabled |
| `token-lifecycle.md:387`, `smoke-tests.md:415` | Vendor tokens can “never” outlive custody | Include unsupported revocation/local deletion and best-effort cleanup after failed custody writes |
| `smoke-tests.md:295` | CAS means refresh-token family is “never burned” | CAS protects stored writes; crash-after-vendor-consumption can still require re-consent |
| `operations.md:12` | `max_versions=2` says no superseded token pairs | It retains up to two versions; current-entry scrubbing is not erasure of all historical token material |
| `smoke-tests.md:65` | Health response example omits custody status | Show current `ok` and `custody` fields and explain that backend outage can remain HTTP 200 |

Also clarify that clipping `granted_scopes` in `refresh.py` does not reduce permissions on the vendor-issued token. Label p99/availability figures as targets unless benchmark evidence is supplied. Describe mass-STALE as an emitted alert signal requiring monitoring integration. Document custody audit-device setup before claiming every read is audited. Replace “every security property verified” with the specific properties covered by each test.

## Priority 2 — make the site easier to consume

The landing page starts with duplicate titles, lab provenance, and normative prose. A newcomer must understand implementation details before finding how to use the service.

Recommended navigation:

| Page | Reader's question |
|---|---|
| Overview | What does this solve, and where does it fit? |
| Quickstart | Can I run it and connect one mock vendor? |
| Integrate with MCP | Who authenticates whom, and how does consent reach the user? |
| API reference | What request/response and error handling do I implement? |
| Deploy and operate | How do I provision, configure, upgrade, monitor, and recover? |
| Security and MCP alignment | What is enforced, by whom, and with what evidence? |
| Internals | How do refresh, races, custody, and coordination work? |

Keep lifecycle diagrams and ADRs as linked detail. Publish `operations.md`, the ADR, and release notes; the generator currently includes only design, lifecycle, and smoke tests. The public site's private-repository link is not sufficient access to these materials.

Specific improvements:

- Lead with a short description, one boundary diagram, and “Run locally,” “Integrate,” and “Operate” entry points. Move extraction history into an About section.
- Make the quickstart one successful journey: start stack → resolve → sign in/connect → retry → revoke, with expected outputs. Label curl as simulating the **trusted gateway**, since it receives vendor tokens.
- Give every heading a stable anchor and provide a local table of contents; replace bare filenames and section-number references with working deep links.
- Fix Markdown spacing: API response bullets in `design.md:118` and smoke-test proof bullets currently collapse into long paragraphs in generated HTML.
- Use one page title; remove the duplicate generator/source H1. Add a skip link, keyboard focus styling, and heading scroll offsets for sticky navigation.
- Provide copy buttons, concise error/action tables, a small glossary, and short textual summaries for diagrams. Validate narrow-screen tables and diagrams before publishing.
- Keep one authoritative API/error reference and link to it from walkthroughs to reduce contradictory copies.

## Priority 2 — prevent future drift

CI currently validates code/schema and runs tests, but has no docs-generation or publishing checks. The public companion repository is updated manually.

Add deterministic generation/freshness checks, link/anchor validation, and a browser smoke check for rendered diagrams and JavaScript failures. Pin Mermaid to a tested exact version instead of the moving `@11` CDN reference. Publish an allowlisted static artifact from a known source commit, verify the deployed artifact, and display software version, source revision, MCP baseline, and last review date.

Recommended delivery order: correct claims and MCP boundaries; add the missing operator content and task-based navigation; then automate freshness and publication. Completion means a new reader can run one successful flow, identify each authorization boundary, and follow every standards claim to its owner, implementation, and test or stated limitation.
