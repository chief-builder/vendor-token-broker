# Operations

## Custody provisioning (OpenBao / Vault, KV v2)

Two mounts, two policies — grant entries are broker read/write; vendor
client credentials are broker read-only, admin write-only. No human read
path to token material.

```sh
bao secrets enable -path=vendor-tokens kv-v2
bao secrets enable -path=vendor-clients kv-v2
bao write vendor-tokens/config max_versions=2   # keep no superseded token pairs
bao policy write broker deploy/openbao-policy.hcl
bao token create -policy=broker -period=24h -orphan -display-name=broker
```

Give the broker the scoped token (`VAULT_TOKEN` or `VAULT_TOKEN_FILE`),
**never root**. Use a **periodic** token as above: the broker renews its
own token at half the remaining TTL, and a periodic token can be renewed
indefinitely. A token with a hard maximum TTL (for example `-ttl=768h`)
can only be renewed up to that maximum; `/healthz` starts failing 10
minutes before it expires, and it must then be replaced and the broker
restarted. The token also needs the built-in `default` policy (attached
unless `-no-default-policy` is given), which grants `lookup-self` and
`renew-self`.

At startup the broker checks its custody token and fetches the hub JWKS
before serving. An unreachable backend or hub is retried for
`STARTUP_TIMEOUT_S`; a rejected token or a JWKS with no keys aborts at
once with a message naming the setting to fix. Write each vendor's confidential client credential on the
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
`VAULT_TOKEN` or `VAULT_TOKEN_FILE`, `REGISTRY_PATH`, `HUB_LOGIN_CLIENT_ID`.
Optional: `HUB_LOGIN_CLIENT_SECRET` (omit for a public client; PKCE is
always used).

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
| `SWEEP_MAX_ENTRIES` | `500` | Entries examined per sweep pass; the next pass resumes where this one stopped |
| `LOCK_TIMEOUT_S` / `LOCK_TTL_MS` | `10` / `20000` | Waiter budget / redis lock TTL. With `COORD_BACKEND=redis`, startup rejects `LOCK_TTL_MS` below `(VENDOR_TIMEOUT_S + 2 × VAULT_TIMEOUT_S) × 1000` |
| `REFRESHING_TTL_S` | `30` | Abandoned-marker takeover threshold |
| `MASS_STALE_THRESHOLD` / `MASS_STALE_WINDOW_S` | `3` / `60` | Uninstall-anomaly page |
| `VAULT_TIMEOUT_S` | `3` | Bounded fail-closed detection |
| `VENDOR_TIMEOUT_S` | `10` | Each vendor HTTP call (metadata, token, userinfo, revocation) |
| `JWKS_TIMEOUT_S` | `5` | Hub JWKS fetch |
| `STARTUP_TIMEOUT_S` | `30` | How long startup waits for an unreachable custody backend or hub JWKS |

## Hub login client (consent)

Consent requires the user to sign in at the hub in the browser that opened
the authorize link (design §6). Register the broker at the hub as an OIDC
client:

- **Redirect URI:** `{BROKER_PUBLIC_URL}/v1/callback/_hub` (exact match).
- **Grant:** authorization code, with PKCE (S256) required; scope `openid`.
- **ID tokens** signed with an algorithm in `HUB_ALGORITHMS`, by keys the
  hub publishes at `HUB_JWKS_URI`.
- **Client auth:** confidential (`HUB_LOGIN_CLIENT_SECRET`, sent as
  `client_secret_post`) or public (no secret).

The broker finds the hub's endpoints at
`{HUB_ISSUER}/.well-known/openid-configuration` and checks at startup that
the document names `HUB_ISSUER`. Serve the broker over https so the
consent binding cookie is marked `Secure`.

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

Added in 1.1, additive only (existing slugs and statuses never change):
`hub-unavailable` (503: the hub JWKS could not be fetched, which is an outage
and not a bad token) and `invalid-request` (400: a malformed resolve body).
A plugin that treats every 5xx as retriable needs no change.

Frozen audit event names: `broker.resolve`, `broker.consent.start`,
`broker.consent.complete`, `broker.consent.fail`, `broker.refresh`,
`broker.stale`, `broker.stale.mass`, `broker.revoke`, `broker.admin.deny`,
`broker.sweep.error`. Added in 1.1 (additive): `broker.custody.renew_failed`.

## Runbook

- **`/healthz`** returns `{"ok", "custody"}`. It fails (503) only for a
  problem with this replica's own custody token: `token-rejected` (revoked,
  expired, or wrong) or `token-expiring` (under 10 minutes left). Replace
  the token and restart the replica. A custody **outage** is reported as
  `"custody": "unreachable"` but stays 200 on purpose: every replica shares
  the outage, and draining or restarting them would discard the cache hits
  that keep serving meanwhile. Alert on the body, not only the status. The
  result is cached for 10 seconds.
- **`broker.custody.renew_failed`**: the broker could not renew its custody
  token and is retrying with backoff (`ttl_remaining_s` says how long is
  left). If the backend is up, check the token's policies and max TTL.
- **`broker.consent.fail` with `reason: login_sub_mismatch`**
  (`security_event: true`): someone opened an authorize link issued to
  another user (`sub` is the link's user, `login_sub` who signed in). One
  event can be a user opening a colleague's link by mistake; repeats from
  one `login_sub`, or links sent to many users, suggest a linking attack.
- **`broker.consent.fail` with `reason: browser_mismatch`**: a consent leg
  was finished in a browser that did not start it (a forwarded callback or
  vendor URL). No code was redeemed.
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
  Each pass lists every vendor's entries, then examines at most
  `SWEEP_MAX_ENTRIES` of them, resuming from where the previous pass
  stopped. A full cycle over all entries fits inside the 10-minute proactive
  band while `total entries ≤ SWEEP_MAX_ENTRIES × 600 / SWEEP_INTERVAL_S`
  (5,000 with the defaults); beyond that, some entries miss the proactive
  refresh and are refreshed lazily on their next resolve instead.
- **Blocking I/O**: custody (hvac) and hub JWKS calls run in worker threads,
  never on the event loop. A frozen custody backend therefore holds one
  thread per in-flight uncached request for up to `VAULT_TIMEOUT_S`, while
  cache hits and `/healthz` keep answering.

## Custody layout and upgrades

Grant entries live at `vendor-tokens/{vendor}/sub-b64.{base64url(sub)}`:
one key per subject, whatever characters the hub puts in `sub` (URIs with
`/` included). Entries written before this encoding sit at
`vendor-tokens/{vendor}/{sub}`; the broker still reads them there and moves
each one to the encoded key on its next write (a refresh, re-consent, or
delete). No migration job is needed. Upgrade all replicas together: an
older replica cannot see entries a newer one has already moved, and would
answer needs-consent for them until it is replaced.

STALE entries keep no token material (both tokens are blanked);
`REVOKE_PENDING` entries keep theirs until the vendor revocation succeeds.

## Multi-replica deployment

Run ≥2 replicas only with `COORD_BACKEND=redis` (see
`deploy/docker-compose.multi.yml` for the shape: shared OpenBao + Redis,
any L4/L7 balancer, no session affinity required). Keep `LOCK_TTL_MS`
above the slowest vendor token round-trip and `REFRESHING_TTL_S` above
`LOCK_TTL_MS`.
