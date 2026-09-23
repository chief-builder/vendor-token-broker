# Changelog

## v1.1.0 — unreleased

Fixes from the September 2026 project review. Existing gateway plugins keep
working unchanged: every wire change below is additive.

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
- Consent never orphans a redeemed grant; custody keeps 2 versions per
  entry (`max_versions`), STALE entries hold no tokens; error `detail` text
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

### Tests and supply chain
- 319 unit tests (offline harness over the real app) and 69 integration
  tests (61 per coordination backend, 7 multi-replica, 1 external)
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
