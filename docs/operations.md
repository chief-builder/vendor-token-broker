# Operations

## Custody provisioning (OpenBao / Vault, KV v2)

Two mounts, two policies — grant entries are broker read/write; vendor
client credentials are broker read-only, admin write-only. No human read
path to token material.

```sh
bao secrets enable -path=vendor-tokens kv-v2
bao secrets enable -path=vendor-clients kv-v2
bao policy write broker deploy/openbao-policy.hcl
bao token create -policy=broker -ttl=768h -display-name=broker
```

Give the broker the scoped token (`VAULT_TOKEN` or `VAULT_TOKEN_FILE`),
**never root**. Write each vendor's confidential client credential on the
admin path:

```sh
bao kv put vendor-clients/github  client_id=… client_secret=…
bao kv put vendor-clients/acmehub client_id=… private_key=@key.pem alg=RS256
```

The credential shape must match the registry's
`token_endpoint_auth_method`: `client_secret_post`/`client_secret_basic`
need `client_secret`; `private_key_jwt` needs `private_key` (PEM, PKCS8)
plus optional `alg` (default RS256) and `kid`.

Custody loss fails closed: resolve returns 503 `vault-unavailable`, with
grace limited to each replica's ≤60s in-memory cache.

## Configuration reference

Required (startup aborts listing every missing name):
`HUB_ISSUER`, `HUB_JWKS_URI`, `BROKER_PUBLIC_URL`, `VAULT_ADDR`,
`VAULT_TOKEN` or `VAULT_TOKEN_FILE`, `REGISTRY_PATH`.

| Variable | Default | Notes |
|---|---|---|
| `HUB_TIER_AUDIENCE` | `mcp://tier/internal` | Exactly one `mcp://tier/*` audience is enforced |
| `HUB_ALGORITHMS` | `PS256,ES256` | Allowlist: PS256/384/512, ES256/384/512, EdDSA. RS256, HMAC, and `none` fail startup |
| `HUB_CONTRACT_VERSION` | `1.0` | `mcp_contract` claim pin |
| `ADMIN_GROUP` | `mcp-platform-admin` | Required member of the `groups` array claim for `/v1/admin/*` |
| `PROBLEM_URN_PREFIX` | `urn:vendor-token-broker` | `type` prefix only; `title` slugs never change |
| `COORD_BACKEND` | `memory` | `redis` required for >1 replica |
| `REDIS_URL` | `redis://localhost:6379/0` | redis profile |
| `REFRESH_BUFFER_S` | `300` | Lazy-refresh band |
| `PROACTIVE_REFRESH_S` | `900` | Sweeper refresh band upper edge |
| `CACHE_TTL_S` | `60` | Per-replica cache and outage grace cap |
| `SWEEP_INTERVAL_S` | `60` | `0` disables the sweeper. Every other timing knob must be ≥ 1 (`CACHE_TTL_S` may be `0`: no cache) |
| `LOCK_TIMEOUT_S` / `LOCK_TTL_MS` | `10` / `15000` | Waiter budget / redis lock TTL |
| `REFRESHING_TTL_S` | `30` | Abandoned-marker takeover threshold |
| `MASS_STALE_THRESHOLD` / `MASS_STALE_WINDOW_S` | `3` / `60` | Uninstall-anomaly page |
| `VAULT_TIMEOUT_S` | `3` | Bounded fail-closed detection |

## Vendor registry review workflow

The registry (`REGISTRY_PATH`) is the vendor allowlist and per-vendor
policy. Treat every change as a reviewed change:

1. Edit a copy of `registry.example.json`.
2. Validate: CI runs `jsonschema` against
   `schemas/vendor-registry.schema.json` on every push; do the same locally.
3. `scope_ceiling` changes require the same sign-off as a token-contract
   change — the ceiling is a security boundary (a caller can never widen
   past it at runtime).
4. Endpoints may be hardcoded **only** where the vendor publishes no
   RFC 8414 metadata (e.g. GitHub); where metadata exists, use
   `auth_metadata_url`.
5. Credentials never live in the registry — only in `vendor-clients/*`.

## Gateway (Kong) wire contract

What the egress plugin reads from the broker — frozen; verified by
`tests/integration/test_wire_compat.py`:

| Broker response | Field(s) read | Plugin behavior |
|---|---|---|
| `200` | `access_token` | Swap upstream `Authorization`; hub JWT never transits to the vendor |
| `404` + `authorize_uri` | `authorize_uri` | 401 `authorization_required` challenge to the client |
| `409` + `authorize_uri` | `authorize_uri` (+`missing_scopes` in body) | 401 step-up re-consent challenge |
| `409` (plain) | `title` | 403 with the title slug (e.g. `revoke-pending`) |
| other 5xx | — | 503 `vendor_unavailable`, retriable |

Frozen `title` slugs: `needs-consent`, `needs-reconsent-scope`,
`invalid-hub-token`, `sub-mismatch`, `unknown-vendor`,
`scope-exceeds-ceiling`, `revoke-pending`, `vendor-unavailable`,
`vault-unavailable`, `coordination-unavailable`, `forbidden`, `no-grant`,
`invalid-transaction`.

Frozen audit event names: `broker.resolve`, `broker.consent.start`,
`broker.consent.complete`, `broker.consent.fail`, `broker.refresh`,
`broker.stale`, `broker.stale.mass`, `broker.revoke`, `broker.admin.deny`,
`broker.sweep.error`.

## Runbook

- **`broker.stale.mass` page**: an org-level app uninstall or credential
  rotation at one vendor. Per-entry recovery is self-service re-consent;
  investigate the vendor-side cause before mass-notifying users.
- **503 `vault-unavailable`**: custody outage; the broker is fail-closed by
  design. Restore the backend; entries and generations are intact (CAS).
- **503 `coordination-unavailable`** (redis profile): refresh path only —
  cache hits keep serving. Restore redis; no state is lost (consent records
  expire and are re-created; the CAS protected custody throughout).
- **Replica died mid-refresh**: nothing to do. The lock TTL expires, the
  abandoned `REFRESHING` marker is taken over, and in the worst case a
  rotating-RT family burns → STALE → the user re-consents. Custody can
  never hold a stale token pair (CAS).
- **Sweeper**: leader-lease holder only (redis profile). `broker.sweep.error`
  events carry the vendor/sub that failed; a bad entry never kills the loop.

## Multi-replica deployment

Run ≥2 replicas only with `COORD_BACKEND=redis` (see
`deploy/docker-compose.multi.yml` for the shape: shared OpenBao + Redis,
any L4/L7 balancer, no session affinity required). Keep `LOCK_TTL_MS`
above the slowest vendor token round-trip and `REFRESHING_TTL_S` above
`LOCK_TTL_MS`.
