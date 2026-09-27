# vendor-token-broker

OAuth credential custodian for third-party SaaS vendors: resolves live
vendor tokens per user for an egress gateway, runs the consent dance,
refreshes single-flight, revokes vendor-first. Extracted from the
`mcp-healthcare-reference` lab (@ `ab699f3`). Normative docs:
`docs/design.md` (behavior), `docs/operations.md` (config, wire contract,
runbook), `docs/adr/0001-redis-coordination.md`, `docs/mcp-gateway.md`
(the MCP gateway).

## Hard invariants — never break these

- **Custodian, not issuer.** Exactly 7 routes; no token minting, no JWKS,
  no issuer signing keys. `tests/unit/test_routes.py` enforces this. (Vendor
  `private_key_jwt` client-assertion keys live in custody as credential
  material, never in config or logs.)
- **Wire freeze.** Resolve statuses 200/404/409/5xx; response fields
  `access_token`, `authorize_uri`, `missing_scopes`; problem `title` slugs;
  audit event names. Existing gateway plugins parse these. Only the URN
  prefix in `type` is configurable. `tests/integration/test_wire_compat.py`
  enforces this.
- **No token material in logs, errors, or audit events** — ids, states,
  and generations only. The token-in-log grep test enforces this.
- **KV-v2 generation CAS is the correctness backstop.** Locks are an
  optimization; never write a token pair after losing a CAS — discard,
  re-read.
- **Fail closed, distinguishably.** Custody outage → 503
  `vault-unavailable` (never "absent" → consent); redis outage → 503
  `coordination-unavailable` on every path that needs redis (refresh,
  consent-link minting, authorize/callbacks, DELETE); resolves that need no
  refresh keep serving.
- `memory` coordination backend must keep exact single-replica semantics;
  multi-replica behavior belongs in the `redis` backend only.

## Commands

```sh
.venv/bin/pip install --require-hashes -r requirements-dev.lock  # once
.venv/bin/pip install --no-deps -e .
ruff check src tests
pytest tests/unit -q                             # offline, no containers

docker compose -f tests/stack/docker-compose.yml up -d --build --wait
pytest tests/integration -m "not external and not multi and not gateway" -q

# multi-replica proof (2 replicas + nginx LB + redis)
docker compose -f tests/stack/docker-compose.yml --profile multi up -d --build --wait
BROKER_URL=http://localhost:8400 BROKER_CONTAINERS=vtb-broker-a,vtb-broker-b \
  pytest tests/integration/test_multi_replica.py -q

# MCP gateway end to end: Keycloak hub, broker-kc, mock-mcp stand-ins, mcp-gateway
docker compose -f tests/stack/docker-compose.yml --profile gateway up -d --build --wait
pytest tests/integration -m gateway -q
```

CI (`.github/workflows/ci.yml`): lint+schema / unit / docker-build /
integration(memory, redis) / gateway / multi. The integration job is a matrix over
`COORD_BACKEND`; each leg brings the stack up with that env var.

## Layout

- `src/token_broker/` — `main.py` (app factory + 7 routes), `config.py`
  (fail-fast dataclass), `hub.py` (hub-JWT re-validation), `hub_login.py`
  (consent-leg hub OIDC login), `lifecycle.py` (startup checks, custody-token
  renewal, `/healthz`), `vendors.py`
  (registry + vendor OAuth legs), `client_auth.py` (post/basic/
  private_key_jwt), `custody.py` (KV-v2 CAS store), `coordination.py`
  (memory/redis backends), `refresh.py` (shared single-flight refresh
  core), `sweeper.py`, `problems.py` (frozen titles), `audit.py`
- `src/mcp_gateway/` — separate service (own `Dockerfile.gateway`,
  `requirements-gateway.lock`, FastMCP 4.0.10; never in the broker image):
  MCP server to clients, MCP client to several vendor MCP servers
  ("upstreams": GitHub, Linear, Atlassian, Cloudflare). `upstreams.json` lists them (name = tool
  prefix, broker vendor, URL, auth scheme, headers, allowlist, snapshot);
  tools are exposed as `<name>_<tool>` plus `connect_<name>`. `server.py`
  (routes, consent elicitation in both protocol eras, auth wiring),
  `clients.py` (hub RFC 8693 exchange, broker resolve/grants per vendor),
  `upstream.py` (fresh session per call), `config.py` (upstreams file
  loading/validation). `snapshots/<name>.json` pin each allowlist's schemas
  so clients see them from startup (2026-07-28 clients only take
  list-changed on subscriptions/listen, which FastMCP 4.0.10 lacks); the
  first connected call per upstream re-reads live schemas and logs drift
  (never adopts it: the list is shared, and Cloudflare personalizes
  descriptions with the user's email and account id; scrub snapshots too).
  Refresh with `UPSTREAM_TOKEN=... .venv/bin/python tools/refresh-tool-snapshot.py <name>`
- `tests/stack/` — self-contained compose: OpenBao (+ init writing a
  scoped token, never root), redis, mock-vendor (hostile: 60s tokens,
  rotating RTs, replay burns the family; its `.../mcp` metadata variant plays
  an MCP server's own sign-in: DCR at `/register`, tokens bound to the
  `resource`, `invalid_target` if a code exchange or refresh omits it,
  `/introspect` for the stand-ins), hub-stub (JWKS, good and bad
  hub JWTs via `POST /_test/token`, OIDC login for the consent leg)
  — plus, in the `gateway` profile, Keycloak (real hub), broker-kc (:8600,
  own OpenBao), mock-mcp (:8330, stand-ins at /github/mcp, /linear/mcp,
  /atlassian/mcp and /cloudflare/mcp accepting only live mock-vendor tokens,
  the last two only tokens issued for them; `/_test/state` records per-call
  upstream, auth scheme, headers and token fingerprints) and mcp-gateway
  (:8500, stand-in upstreams file `tests/stack/gateway/stand-in-upstreams.json`:
  github -> vendor mockhub, linear -> vendor mockhub-jwt, atlassian ->
  mockhub-atlassian, cloudflare -> mockhub-cloudflare)
- `registry.example.json` + `schemas/vendor-registry.schema.json` —
  registry changes are reviewed changes; `scope_ceiling` is a security
  boundary (for Atlassian and Cloudflare it is the only thing keeping the
  upstream read-only). Vendors whose MCP server runs its own sign-in carry
  `auth_metadata_url` + `resource` (RFC 8707, sent on authorize, code
  exchange and refresh); the broker is registered with them once via
  `tools/register-mcp-client.py` — never re-register (orphans every
  connection). Cloudflare's client secret expires (2026-12-26).

## Gotchas

- Mock vendor tokens live 60s < `REFRESH_BUFFER_S` (300), so **every**
  integration resolve takes the refresh path. To test the cache path or
  invalidation, hand-write a long-lived entry via OpenBao root
  (`http://localhost:8210`, token `root`) — see `test_multi_replica.py`.
- The mock widens scopes on refresh (GitHub-class); assert 409
  `needs-reconsent-scope` before any refresh happens.
- Test helpers are deliberately NOT in conftest.py (`tests/unit/
  unit_helpers.py`, `tests/integration/stack.py`) — bare `conftest`
  imports collide when both suites are collected together.
- Integration asserts read audit events from `docker logs` of `vtb-broker`
  (or replicas via `BROKER_CONTAINERS`); container names matter.
- Gateway profile: Keycloak's public issuer is `http://localhost:8180`
  (Claude Code only sends OAuth credentials over plain http to exactly
  localhost); containers reach it as `http://keycloak:8180` and get
  internal endpoints (`--hostname-backchannel-dynamic`), so broker-kc sets
  `HUB_DISCOVERY_URL` and the gateway uses internal JWKS/token URLs.
  Keycloak sets Secure cookies over http, so test browsers must send Secure
  cookies to localhost like real browsers do (`keycloak_stack.browser_session`).
  The realm allows anonymous dynamic client registration only for localhost
  redirect URIs (stock MCP clients such as Claude Code register themselves)
  and requires consent for such clients. Switch the gateway between the
  stand-in and real GitHub with `up -d --no-deps mcp-gateway` (the e2e suite
  pauses broker-kc, so its health can lag and block dependency gating). broker-kc has its own
  OpenBao so its sweeper never shares custody with `broker`.
- Use `docker pause` (not stop) to simulate OpenBao/mock outages — dev-mode
  OpenBao state is in-memory and a restart wipes provisioning.
- App factory pattern: `uvicorn --factory token_broker.main:create_app`.
  Nothing reads env at import time; unit tests build `Config` directly.
