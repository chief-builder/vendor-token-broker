# Changelog

## v1.1.0 — unreleased

Fixes from the September 2026 project review. Existing gateway plugins keep
working unchanged: every wire change below is additive.

### Threat model and vendor check (2026-09-30)
- `THREAT_MODEL.md`: assets, trust boundaries, threats, mitigations,
  residual risks, and the test behind each control
- New tests: each gateway call runs as the signed-in person (Bob never gets
  Alice's connection); hidden write tools cannot be called; a token revoked
  at the vendor fails closed to consent; tool results carry no token
- Vendor MCP servers and auth flows re-checked against vendor docs and live
  metadata: all current. Docs corrected: GitHub now serves (undocumented)
  RFC 8414 metadata; Atlassian's admin menu is "Rovo MCP server"

### Hardening review (2026-09-30)
See `AUDIT.md` for the findings behind each change.
- **Python 3.14** (was 3.12, now security-only): `requires-python >=3.14`,
  `.python-version`, `python:3.14-slim` images pinned by digest. The three
  locks were regenerated and now agree (PyJWT 2.15.1, uvicorn 0.54.0,
  fastapi 0.142.2); `pip-audit` finds no known vulnerabilities
- **Fix (gateway):** an upstream MCP server's 401 was never recognized (the
  MCP SDK reports it as a generic error), so a user whose vendor token was
  revoked was told to retry instead of to reconnect. The HTTP status is now
  recorded per call (`UpstreamRejected`)
- **Fix (gateway):** malformed hub or broker answers (a 200 without a
  token, non-JSON) map to the typed handoff/unavailable errors
- **Fix (deploy):** the deploy compose files now pass the whole `.env` to
  the broker (vendor `enabled_env` ids, `HUB_LOGIN_HINT`, knobs were
  dropped before) and document the token-file ownership (UID 65534)
- Registry: `revocation.type` is an enum (`rfc7009`, `github_grant`), and
  `revocation.grant_url` overrides GitHub's grant-deletion URL (GitHub
  Enterprise Server)
- The hub JWKS cache window is explicit (`JWKS_CACHE_S`, 300 s) and tested
  on both sides; the docs no longer claim removed keys stop at once
- `main.py` split into `broker`, `resolve`, `consent`, and `grants` (routes
  unchanged); `src/` passes mypy; code formatted with `ruff format`
- New unit tests for security paths (issuer checks, single-use state,
  fail-closed coordination, self-service DELETE, grant listing, the RFC 8693
  exchange, GitHub grant revocation); coverage 91% -> 95%
- CI: actions moved off Node 20 and SHA-pinned; least-privilege permissions,
  concurrency, timeouts; format, mypy, coverage floor, build, `pip-audit`,
  and link-check jobs; CodeQL and Dependabot; a `Makefile` for all of it
- Test stack: exact image versions pinned by digest (OpenBao 2.7.0, Redis
  8.10.2, nginx 1.30.5, curl 8.22.0, Keycloak 26.7.4), stub images built
  from the hashed lock, project name `vtb`
- Docs: README rebuilt (why, architecture, quickstart from a clean clone,
  configuration, tests, status and limitations); wrong claims fixed (the
  repo is public; test counts; key-cache window); `SECURITY.md`,
  `CONTRIBUTING.md`, templates; the stale `docs-site-review.md` removed

### Wire additions (additive)
- Problem titles `invalid-request` (400, malformed resolve body) and
  `hub-unavailable` (503, hub JWKS unreachable; was a misleading 401)
- Audit event `broker.custody.renew_failed`; `broker.consent.fail` reasons
  `login_sub_mismatch` and `browser_mismatch`; audit fields
  `min_ttl_clamped_from`, `short_ttl`, `scope_widened`, `new_grant_revoked`
- `/healthz` body `{"ok", "custody"}`; `DELETE /v1/grants` adds
  `"vendor_revocation": "unsupported"` for vendors without revocation

### Security
- **Consent is bound to the user and the browser (breaking for operators).**
  Before, anyone holding an authorize link could complete it, so an
  attacker could send their own link to a victim and have the victim's
  vendor account stored under the attacker's `sub`. Now `/v1/authorize`
  sends the browser to sign in at the hub (OIDC code + PKCE + nonce) and
  requires the signed-in `sub` to be the link's; an HttpOnly cookie binds
  every leg to the browser that opened the link; links are single use and
  expire after 5 minutes. Requires `HUB_LOGIN_CLIENT_ID` and a hub client
  registration (`docs/operations.md`). The hub returns to
  `/v1/callback/_hub`, so the route table is unchanged
- Exactly 7 routes served: FastAPI's `/docs`, `/redoc`, `/openapi.json`
  are off, and the route test audits the router rather than the schema
- Admin group must be a list claim (a string claim was a substring match)
- Hub signing keys are no longer cached forever (removed keys stop working)
- `HUB_ALGORITHMS` is an allowlist (no RS256, HMAC, or `none`)
- Consent never orphans a redeemed grant; operators set `max_versions=2`
  on custody entries (provisioning, `docs/operations.md`), STALE entries
  hold no tokens; error `detail` text
  never carries backend hostnames; consent pages send no-store/no-referrer

### Reliability
- Custody (hvac) and JWKS I/O run off the event loop: cache hits and
  `/healthz` keep answering during a custody stall
- Startup verifies the custody token and hub JWKS; the custody token is
  renewed; `/healthz` fails only for this replica's own token problems
- Single-flight holds when `min_ttl_s` exceeds the vendor's token lifetime
  (`min_ttl_s` is capped at `REFRESH_BUFFER_S`)
- Non-expiring tokens are never refreshed; deletes hold the refresh lock;
  a waiting resolve never refreshes an entry parked for revocation
- Every failure maps to a deliberate status and audit line (no bare 500s)
- Custody keys are encoded (`sub-b64.…`), so subjects with `/` work
  everywhere; older entries migrate on their next write
- Bounded in-process state; atomic sweep-lease renewal; sweep budget
  (`SWEEP_MAX_ENTRIES`); consent-record cleanup without the sweeper

### Configuration
- New required: `HUB_LOGIN_CLIENT_ID` (optional `HUB_LOGIN_CLIENT_SECRET`)
- New: `STARTUP_TIMEOUT_S`, `SWEEP_MAX_ENTRIES`, `VENDOR_TIMEOUT_S`,
  `JWKS_TIMEOUT_S`. `LOCK_TTL_MS` default 15000 → 20000, and with redis it
  must cover `VENDOR_TIMEOUT_S + 2 × VAULT_TIMEOUT_S`
- Timing knobs must be ≥ 1 (`SWEEP_INTERVAL_S` and `CACHE_TTL_S` may be 0)
- Operators: use a periodic custody token; set `max_versions=2` on
  `vendor-tokens`; upgrade all replicas together (custody key migration)
- `HUB_LOGIN_HINT` (`sub` default, `none` for real IdPs, which pre-fill the
  hint as a username) and `HUB_DISCOVERY_URL` (fetch hub discovery from an
  internal address; the document must still name `HUB_ISSUER`)

### MCP gateway (new, separate service)
- `src/mcp_gateway/`: an MCP server to clients and an MCP client to
  GitHub's MCP server, built on FastMCP 4.0.10 with its own image
  (`Dockerfile.gateway`) and hash-checked `requirements-gateway.lock`; the
  broker image and route table are unchanged
- MCP authorization at the gateway: protected-resource metadata, 401
  challenges naming the required scope, audience/issuer/algorithm/scope
  checks, and a `gateway.auth` audit line for every refused token
- Identity handoff by RFC 8693 token exchange at the hub: the MCP token is
  never forwarded to the broker or GitHub
- Consent inside the tool call: URL-mode elicitation on 2025-11-25 and
  2026-07-28 (multi-round-trip) clients, a link in the error otherwise;
  waits on the grant list, not resolve
- Read-only tool allowlist with pinned schemas (`snapshots/<name>.json`,
  refreshed by `tools/refresh-tool-snapshot.py <name>`) reconciled with
  the live schemas on the first connected call
- `gateway` Compose profile: Keycloak 26.7.4 as a real hub, a broker that
  trusts it, the gateway, and a GitHub MCP stand-in; `tools/mcp-demo-client.py`
- Tested manually end to end with Claude Code 2.1.283 against GitHub's MCP
  server
- **Several MCP servers behind one gateway**, listed in `upstreams.json`
  (name, broker vendor, URL, auth scheme, headers, protocol, allowlist,
  snapshot); `GATEWAY_UPSTREAMS` and `GATEWAY_ENABLED_UPSTREAMS` select
  them. Each has its own `connect_<name>` tool, consent, and catalog
- **Linear**: 13 read-only tools through `https://mcp.linear.app/mcp/readonly`,
  with a `linear` broker registry entry (enabled by `LINEAR_CLIENT_ID`)
- **Breaking (pre-release):** tools are now prefixed (`github_get_me`,
  `linear_list_issues`); the `VENDOR` and `UPSTREAM_*` gateway settings are
  replaced by the upstreams file
- Upstream errors are logged with a `detail` (token scrubbed); the test
  stand-in is now `tests/stack/mock-mcp` serving `/github/mcp` and `/linear/mcp`
- **Atlassian** (8 read and search tools for Jira and Confluence) and
  **Cloudflare** (`search`, `docs`, `execute`, read-only by scopes alone):
  MCP servers that run their own sign-in. New optional registry field
  `resource` (RFC 8707), sent on authorize, code exchange, and refresh;
  `tools/register-mcp-client.py` registers the broker once per vendor
  (RFC 7591) and refuses to register again unless forced
- **Saved tool lists win (behavior change):** a live schema that differs
  from the snapshot is logged as `changed` but no longer replaces it, since
  the list is shared and Cloudflare personalizes a description with the
  user's email and account ID; a test refuses snapshots holding either
- Test stack: mock-vendor plays an MCP sign-in (dynamic registration,
  resource-bound tokens, `invalid_target`), and mock-mcp adds
  `/atlassian/mcp` and `/cloudflare/mcp`, which refuse tokens issued for
  other servers
- **`disconnect_<service>` tools**: the gateway calls the broker's
  self-service `DELETE /v1/grants/{vendor}/{sub}` with the caller's hub JWT
  (vendor revoke first); outcomes `revoked`, `unsupported`,
  `not-connected`, `pending` are explained to the user and audited as
  `gateway.disconnect`. No new broker route
- Fix: a broker 5xx or outage while the gateway waits for someone to finish
  connecting is a retryable error, not a consent timeout (401: handoff error)
- Fix: the gateway's `HUB_ALGORITHM` uses the broker's allowlist (RS384 and
  RS512 were accepted); the unused `claude-code` client is gone from the
  Keycloak test realm
- Fix: the sweeper's REVOKE_PENDING retry could delete a grant written by
  a re-consent while it was revoking (the new vendor token was then never
  revoked). It now holds the entry lock, deletes only the version it
  revoked, and consent writes under the same lock
- **Linear moves to its MCP server's own sign-in**: the broker registers
  with `mcp.linear.app` (DCR), and tokens are bound to the read-only MCP
  server (`resource`), which the full server refuses. New registry field
  `resource_on_refresh` (Linear refuses `resource` on refresh). The client
  secret expires after 90 days
- **Revocation cancels the access token too**: RFC 7009 vendors get the
  access token, then the refresh token (Linear keeps an access token alive
  for up to 24 h after its refresh token is revoked). A refused
  access-token revocation is ignored; the refresh token's answer decides
- Tests: the 20-parallel single-flight tests switch the mock vendor to
  long-lived tokens for the burst, so they no longer see a legitimate
  second refresh (an occasional CI failure)

### Tests and supply chain
- 507 unit tests (offline harness over the real app; 95% line coverage)
  and 123 integration tests (64 per coordination backend, 7 multi-replica,
  47 gateway profile, 5 external), counted 2026-09-30
- Hash-pinned `requirements.lock` / `requirements-dev.lock`; base image
  pinned by digest; GitHub Actions pinned by commit SHA

## v1.0.0 — 2026-07-17

Initial release: the Vendor Token Broker extracted from the source lab
(`mcp-healthcare-reference` @ `ab699f3`) as a standalone, hardened project.

### Carried over (behavior-preserving)
- 7-route no-issuance surface; hub-JWT re-validation (PS256/ES256 pinned,
  exactly one tier audience, contract version)
- Consent dance: PKCE S256, single-use sub-bound `state`, RFC 9207 `iss`
  (+omission) mix-up defense, registry scope ceilings with 409 step-up
- Single-flight refresh with KV-v2 generation CAS; STALE lifecycle with
  mass-STALE paging; revoke-at-vendor-first (RFC 7009) with
  `REVOKE_PENDING` retry; fail-closed custody (503, ≤60s cache grace)
- id-only audit vocabulary (frozen event names)

### Hardened during extraction
- **Multi-replica profile** (`COORD_BACKEND=redis`): distributed
  single-flight lock (SET NX PX + compare-and-DEL), persisted `REFRESHING`
  with abandoned-marker takeover, shared single-use consent state (no
  session affinity), sweep leader lease + jitter, pub/sub cache
  invalidation, 503 `coordination-unavailable` fail-closed semantics
- **Vendor client-auth matrix**: `client_secret_basic` and
  `private_key_jwt` (RFC 7523) alongside `client_secret_post`
- Fail-fast configuration (lab defaults removed; all missing names listed
  at once); configurable problem-URN prefix; `VAULT_TOKEN_FILE` support
- Fixed: a bare `Bearer ` authorization header now yields 401, not 500

### Shipped with the repo
- Self-contained test stack (OpenBao + scoped-token init, Redis,
  mock vendor with rotating RTs + assertion verification, hub-issuer stub)
- 70 unit + 54 integration/multi tests, incl. the wire-compat freeze test
  and the multi-replica proof; CI matrix: lint+schema / unit / docker /
  integration(memory, redis) / multi
