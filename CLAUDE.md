# vendor-token-broker

OAuth credential custodian for third-party SaaS vendors: resolves live
vendor tokens per user for an egress gateway, runs the consent dance,
refreshes single-flight, revokes vendor-first. Extracted from the
`mcp-healthcare-reference` lab (@ `ab699f3`). Normative docs:
`docs/design.md` (behavior), `docs/operations.md` (config, wire contract,
runbook), `docs/adr/0001-redis-coordination.md`.

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
pytest tests/integration -m "not external and not multi" -q

# multi-replica proof (2 replicas + nginx LB + redis)
docker compose -f tests/stack/docker-compose.yml --profile multi up -d --build --wait
BROKER_URL=http://localhost:8400 BROKER_CONTAINERS=vtb-broker-a,vtb-broker-b \
  pytest tests/integration/test_multi_replica.py -q
```

CI (`.github/workflows/ci.yml`): lint+schema / unit / docker-build /
integration(memory, redis) / multi. The integration job is a matrix over
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
- `tests/stack/` — self-contained compose: OpenBao (+ init writing a
  scoped token, never root), redis, mock-vendor (hostile: 60s tokens,
  rotating RTs, replay burns the family), hub-stub (JWKS, good and bad
  hub JWTs via `POST /_test/token`, OIDC login for the consent leg)
- `registry.example.json` + `schemas/vendor-registry.schema.json` —
  registry changes are reviewed changes; `scope_ceiling` is a security
  boundary

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
- Use `docker pause` (not stop) to simulate OpenBao/mock outages — dev-mode
  OpenBao state is in-memory and a restart wipes provisioning.
- App factory pattern: `uvicorn --factory token_broker.main:create_app`.
  Nothing reads env at import time; unit tests build `Config` directly.
