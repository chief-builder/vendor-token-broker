# Deploy and operate

This page shows how to set up and run the broker in a private deployment.

- To try it on your own machine, use the [quickstart](quickstart.md).
- For how callers use the broker, see the [API reference](api.md).
- Before rollout, read the [security limits](security.md#known-limitations).
- If you are deploying the [MCP gateway](mcp-gateway.md), you need the broker too. The gateway section below covers what changes.

## Set up token storage (OpenBao / Vault, KV v2)

The broker keeps vendor tokens in a secrets store (OpenBao or Vault, KV v2 engine). You create two storage paths ("mounts"), each with its own access rules:

| Mount | Holds | Broker access | Admin access |
|---|---|---|---|
| `vendor-tokens` | Each user's vendor tokens ("grant entries") | Read and write | — |
| `vendor-clients` | Each vendor's client credentials (client ID and secret or key) | Read only | Write only |

The supplied broker policy does not limit what storage administrators can read. Use your own deployment policies to restrict human read access.

```sh
bao secrets enable -path=vendor-tokens kv-v2
bao secrets enable -path=vendor-clients kv-v2
bao write vendor-tokens/config max_versions=2   # retain up to two credential versions
bao policy write broker deploy/openbao-policy.hcl
bao token create -policy=broker -period=24h -orphan -display-name=broker
```

### The broker's storage token

- Give the broker the scoped token from the last command, in `VAULT_TOKEN` or `VAULT_TOKEN_FILE`. **Never give it the root token.**
- Use a **periodic** token, as shown above. The broker renews its own token at half the remaining TTL. A periodic token can be renewed forever.
- A token with a hard maximum TTL (for example `-ttl=768h`) can only be renewed up to that maximum. `/healthz` starts failing 10 minutes before it expires. You must then replace the token and restart the broker.
- The token also needs the built-in `default` policy. It grants `lookup-self` and `renew-self`. The policy is attached unless you pass `-no-default-policy`.

### Storage auditing

Turn on an audit device in the storage backend. Check that read events reach your audit sink. The broker's own audit events do not replace storage auditing.

### Startup checks

Before it serves requests, the broker checks three things: its storage token, the hub's signing keys (JWKS), and the hub's OIDC discovery document. The hub is your company's sign-in service (identity provider).

- If the storage backend or hub is unreachable, the broker retries for `STARTUP_TIMEOUT_S`.
- If the token is rejected, or the JWKS has no keys, startup stops at once. The message names the setting to fix.

### Vendor client credentials

Write each vendor's confidential client credential on the admin path:

```sh
bao kv put vendor-clients/github  client_id=… client_secret=…
bao kv put vendor-clients/linear  client_id=… client_secret=…
bao kv put vendor-clients/acmehub client_id=… private_key=@key.pem alg=RS256
```

A vendor with `enabled_env` in the registry (GitHub: `GITHUB_CLIENT_ID`, Linear: `LINEAR_CLIENT_ID`) is only active when that variable is set on the broker.

The credential must match the vendor's `token_endpoint_auth_method` in the registry:

| `token_endpoint_auth_method` | Needs |
|---|---|
| `client_secret_post` or `client_secret_basic` | `client_secret` |
| `private_key_jwt` | `private_key` (PEM, PKCS8). Optional: `alg` (default RS256) and `kid` |

### If storage goes down

The broker fails closed. Resolve returns 503 `vault-unavailable`. The only grace is each replica's in-memory cache (`CACHE_TTL_S`, default 60s).

## Configuration reference

The broker reads its settings from environment variables.

**Required.** If any are missing, startup stops and lists every missing name:
`HUB_ISSUER`, `HUB_JWKS_URI`, `BROKER_PUBLIC_URL`, `VAULT_ADDR`,
`VAULT_TOKEN` or `VAULT_TOKEN_FILE`, `REGISTRY_PATH`, `HUB_LOGIN_CLIENT_ID`.

**Optional, no default value shown in the table:**

- `HUB_DISCOVERY_URL`: where to fetch the hub's OIDC discovery document. Use it when the broker reaches the hub by an internal URL. Defaults to `HUB_ISSUER/.well-known/openid-configuration`. The document must still name `HUB_ISSUER`.
- `HUB_LOGIN_HINT`: `sub` (the default) sends the user's subject as the OIDC `login_hint`. Set `none` for a real IdP such as Keycloak, which expects a username.
- `HUB_LOGIN_CLIENT_SECRET`: leave it out for a public client. The broker always uses PKCE.

**Optional, with defaults:**

| Variable | Default | Notes |
|---|---|---|
| `HUB_TIER_AUDIENCE` | `mcp://tier/internal` | The hub token must have exactly one `mcp://tier/*` audience |
| `HUB_ALGORITHMS` | `PS256,ES256` | Allowed: PS256/384/512, ES256/384/512, EdDSA. RS256, HMAC, and `none` fail startup |
| `HUB_CONTRACT_VERSION` | `1.0` | Required value of the `mcp_contract` claim |
| `ADMIN_GROUP` | `mcp-platform-admin` | Callers of `/v1/admin/*` must have this in their `groups` array claim |
| `PROBLEM_URN_PREFIX` | `urn:vendor-token-broker` | Changes only the `type` prefix. `title` slugs never change |
| `COORD_BACKEND` | `memory` | Use `redis` for more than 1 replica |
| `REDIS_URL` | `redis://localhost:6379/0` | Used by the redis backend |
| `REFRESH_BUFFER_S` | `300` | A resolve refreshes the token if it expires within this many seconds |
| `PROACTIVE_REFRESH_S` | `900` | Upper edge of the window where the sweeper refreshes tokens ahead of time |
| `CACHE_TTL_S` | `60` | Per-replica cache lifetime, and so the most grace during a storage outage. The broker does not cap it. Keep it ≤ 60 to keep the documented bound |
| `SWEEP_INTERVAL_S` | `60` | `0` turns off the sweeper. Every other timing setting must be ≥ 1 (`CACHE_TTL_S` may be `0`: no cache) |
| `SWEEP_MAX_ENTRIES` | `500` | Entries checked per sweep pass. The next pass starts where this one stopped |
| `LOCK_TIMEOUT_S` / `LOCK_TTL_MS` | `10` / `20000` | How long a waiter waits for the lock / how long a redis lock lives. With `COORD_BACKEND=redis`, startup rejects `LOCK_TTL_MS` below `(VENDOR_TIMEOUT_S + 2 × VAULT_TIMEOUT_S) × 1000` |
| `REFRESHING_TTL_S` | `30` | After this, another replica may take over an abandoned refresh marker |
| `MASS_STALE_THRESHOLD` / `MASS_STALE_WINDOW_S` | `3` / `60` | How many entries at one vendor must go stale within the window to raise the uninstall alert (`broker.stale.mass`) |
| `VAULT_TIMEOUT_S` | `3` | Time limit on storage calls, so an outage is detected quickly and fails closed |
| `VENDOR_TIMEOUT_S` | `10` | Time limit on each vendor HTTP call (metadata, token, userinfo, revocation) |
| `JWKS_TIMEOUT_S` | `5` | Time limit on fetching the hub JWKS |
| `STARTUP_TIMEOUT_S` | `30` | How long startup waits for an unreachable storage backend, hub JWKS, or hub OIDC discovery |

## Hub login client (consent)

When a user connects a vendor ("consent"), they must sign in at the hub in the same browser that opened the authorize link (design §6). For this, register the broker at the hub as an OIDC client:

- **Redirect URI:** `{BROKER_PUBLIC_URL}/v1/callback/_hub` (exact match).
- **Grant:** authorization code, with PKCE (S256) required. Scope `openid`.
- **ID tokens:** signed with an algorithm in `HUB_ALGORITHMS`, by keys the hub publishes at `HUB_JWKS_URI`.
- **Client auth:** confidential (`HUB_LOGIN_CLIENT_SECRET`, sent as `client_secret_post`) or public (no secret).

The broker finds the hub's endpoints at `{HUB_ISSUER}/.well-known/openid-configuration`. At startup it checks that this document names `HUB_ISSUER`.

Serve the broker over https. The consent cookie that ties the flow to one browser is then marked `Secure`.

## Vendor registry review workflow

The registry (`REGISTRY_PATH`) lists the allowed vendors and each vendor's policy. Treat every change as a reviewed change:

1. Edit a copy of `registry.example.json`.
2. Check it against `schemas/vendor-registry.schema.json` with `jsonschema`. CI does this on every push. Do the same locally.
3. Get the same sign-off for a `scope_ceiling` change as for a token-contract change. The ceiling is a security boundary: a caller can never go past it at runtime.
4. Hardcode endpoints **only** when the vendor publishes no authorization server metadata (e.g. GitHub). When metadata exists, use `auth_metadata_url`. (Standards: RFC 8414.)
5. Never put credentials in the registry. They live only in `vendor-clients/*`.

## Gateway contract

The gateway plugin that calls the broker lives outside this repository. It depends on fixed response fields and problem titles.

- For the frozen response fields and problem titles, see the [API reference](api.md) and the [legacy gateway mapping](api.md#legacy-gateway-mapping).
- For current protocol boundaries and the proposed consent adapter, see [Integrate with MCP](mcp-integration.md).

Frozen audit event names: `broker.resolve`, `broker.consent.start`,
`broker.consent.complete`, `broker.consent.fail`, `broker.refresh`,
`broker.stale`, `broker.stale.mass`, `broker.revoke`, `broker.admin.deny`,
`broker.sweep.error`. Added in 1.1 (additive): `broker.custody.renew_failed`.

## Runbook

Each entry says what you see and what to do.

### `/healthz` returns 503

`/healthz` returns `{"ok", "custody"}`. It returns 503 only when this replica's own storage token has a problem:

- `token-rejected`: the token is revoked, expired, or wrong.
- `token-expiring`: under 10 minutes left.

**Do this:** replace the token and restart the replica.

### `/healthz` body says `"custody": "unreachable"`

The storage backend is down. `/healthz` still returns 200 on purpose. Every replica shares the outage. Draining or restarting them would throw away the cache hits that keep serving meanwhile.

**Do this:** restore the storage backend. Alert on the body, not only the status code. The health result is cached for 10 seconds.

### `broker.custody.renew_failed`

The broker could not renew its storage token. It keeps retrying with backoff. `ttl_remaining_s` says how long is left.

**Do this:** if the backend is up, check the token's policies and max TTL.

### `broker.consent.fail` with `reason: login_sub_mismatch`

This event has `security_event: true`. Someone opened an authorize link issued to another user. `sub` is the user the link was for. `login_sub` is who signed in.

**Do this:** look at the pattern.

- One event can be a user opening a colleague's link by mistake.
- Repeats from one `login_sub`, or links sent to many users, suggest a linking attack.

### `broker.consent.fail` with `reason: browser_mismatch`

Someone finished a consent step in a browser that did not start it, for example through a forwarded callback or vendor URL. The broker did not redeem the code.

### `broker.stale.mass`

Many entries at one vendor went stale at once. The likely cause is an org-level app uninstall or credential rotation at that vendor. The broker does not send pages itself.

**Do this:**

1. Set up monitoring to route this event to on-call.
2. Investigate the cause at the vendor before you notify users in bulk.
3. Each user recovers by connecting again (re-consent) themselves.

### 503 `vault-unavailable`

The storage backend is down. The broker fails closed by design.

**Do this:** restore the backend. Entries and generations stay intact (the storage write check, CAS, protects them).

### 503 `coordination-unavailable` (redis backend)

Redis is down. Operations that need coordination fail, including refresh and access to consent state. Usable cache hits keep serving.

**Do this:** restore Redis. Consent records lost with Redis need a fresh browser flow. Stored credentials stay safe in storage.

### A replica died mid-refresh

**Do nothing.** The broker recovers on its own:

1. The lock TTL expires.
2. Another replica takes over the abandoned `REFRESHING` marker.
3. In the worst case, a rotating refresh-token family burns. The entry goes STALE and the user re-consents.

CAS stops a losing writer from overwriting a newer version. It cannot recover credentials already used up at the vendor.

### Sweeper errors or missed proactive refreshes

The sweeper refreshes tokens before they expire. With the redis backend, only the replica holding the leader lease runs it.

- `broker.sweep.error` events carry the vendor/sub that failed. A bad entry never stops the loop.
- Each pass lists every vendor's entries, then checks at most `SWEEP_MAX_ENTRIES` of them. The next pass starts where the previous one stopped.
- A full cycle over all entries fits in the 10-minute proactive window while `total entries ≤ SWEEP_MAX_ENTRIES × 600 / SWEEP_INTERVAL_S` (5,000 with the defaults).
- Past that, some entries miss the proactive refresh. They are refreshed on their next resolve instead.

### Slow requests while storage is frozen

Storage (hvac) and hub JWKS calls run in worker threads, never on the event loop. A frozen storage backend holds one thread per in-flight uncached request for up to `VAULT_TIMEOUT_S`. Cache hits and `/healthz` keep answering.

## Storage layout and upgrades

Grant entries live at `vendor-tokens/{vendor}/sub-b64.{base64url(sub)}`. There is one key per subject, whatever characters the hub puts in `sub` (including URIs with `/`).

Older entries sit at `vendor-tokens/{vendor}/{sub}`:

- The broker still reads them there.
- It moves each one to the encoded key on its next write (a refresh, re-consent, or delete).
- You need no migration job.

**Upgrade all replicas together.** An older replica cannot see entries a newer one has already moved. It answers needs-consent for them until you replace it.

What stays in storage:

- A current STALE entry holds no tokens (both are blanked). Older retained KV versions may still hold credentials.
- `REVOKE_PENDING` entries keep their tokens until the vendor revocation succeeds.

## MCP gateway

The [MCP gateway](mcp-gateway.md) is a separate deployment (`Dockerfile.gateway`, `requirements-gateway.lock`). It has its own configuration, hub requirements, audit events, and limitations: see [MCP gateway](mcp-gateway.md#configuration).

Two broker settings matter when you run it with a real IdP:

- If the broker reaches the hub by an internal address that differs from the public issuer (as in the Keycloak profile), set `HUB_DISCOVERY_URL`.
- Set `HUB_LOGIN_HINT=none` for a real IdP.

## Multi-replica deployment

Run 2 or more replicas only with `COORD_BACKEND=redis`. See `deploy/docker-compose.multi.yml` for the shape: shared OpenBao and Redis, any L4/L7 load balancer, no session affinity needed.

Timing rules:

- Keep `LOCK_TTL_MS` above the slowest vendor token round-trip plus storage overhead.
- `REFRESHING_TTL_S` should be larger than `LOCK_TTL_MS / 1000`. Watch the units: seconds versus milliseconds.

Sweep leader handoff: a stopped sweep leader can keep its Redis lease for up to twice its sweep interval (120 seconds with defaults). A successor must then reach its next sweep attempt. Allow for that handoff when you change profiles or timing settings. The [test guide](quickstart.md#run-the-automated-checks) explains how to keep standalone and multi-replica runs apart.
