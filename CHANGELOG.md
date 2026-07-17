# Changelog

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
