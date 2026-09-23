# vendor-token-broker

OAuth credential custodian for third-party SaaS vendors: acquires,
custodies, refreshes, and revokes vendor tokens per enterprise user, and
resolves them per-request for an egress gateway.

**Custodian, not issuer.** The broker holds no signing keys and exposes no
token-minting or JWKS endpoint — the route table is audited for exactly that
on every push (`tests/unit/test_routes.py`).

## What it does

- **Resolve** (`POST /v1/tokens/resolve`): the egress gateway presents the
  caller's hub JWT (independently re-validated: PS256/ES256 pinned, issuer,
  exactly one tier audience, contract version) and gets back a live vendor
  access token — refreshed inline when needed, guaranteed for `min_ttl_s`.
- **Consent** (`/v1/authorize/{vendor}` → vendor AS → `/v1/callback/{vendor}`):
  PKCE S256, single-use sub-bound `state`, RFC 9207 `iss` validation
  including the omission case, scopes capped by the registry ceiling.
- **Lifecycle**: only one request refreshes a user's credential at a time
  (single-flight), and generation compare-and-swap prevents an older token
  pair from overwriting a newer one. Definitively invalid grants become
  `STALE`, require the user to reconnect, and can trigger an organization-wide
  mass-STALE alert. Deletion revokes the credential at the vendor first
  (RFC 7009) before removing it locally. If the custody backend is unavailable,
  the broker uses only its ≤60-second cache grace and then fails closed with 503
  rather than treating the grant as missing or supplying an unverified token.
- **Client auth to vendors**: `client_secret_post`, `client_secret_basic`,
  or `private_key_jwt` (RFC 7523), per registry entry.

## Quickstart (self-contained, no external dependencies)

Requires Docker and Python 3.12.

```sh
git clone <this-repo> vendor-token-broker && cd vendor-token-broker
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-deps -e .

# Bring up the acceptance stack: OpenBao (+ scoped-token init), Redis,
# a hostile mock vendor AS, a hub-issuer stub, and the broker.
docker compose -f tests/stack/docker-compose.yml up -d --build --wait

.venv/bin/pytest tests/unit tests/integration -m "not external and not multi"
```

Multi-replica proof (two redis-coordinated replicas behind round-robin nginx):

```sh
docker compose -f tests/stack/docker-compose.yml --profile multi up -d --build --wait
BROKER_URL=http://localhost:8400 BROKER_CONTAINERS=vtb-broker-a,vtb-broker-b \
  .venv/bin/pytest tests/integration/test_multi_replica.py
```

## Dependencies

Installs are hash-pinned: `requirements.lock` (runtime + redis, used by the
Docker image) and `requirements-dev.lock` (adds the dev tools, used by CI).
Both are generated from `pyproject.toml` and work on every platform. After
changing a dependency in `pyproject.toml`, regenerate them:

```sh
uv pip compile pyproject.toml --extra redis --universal --python-version 3.12 \
  --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra redis --extra dev --universal --python-version 3.12 \
  --generate-hashes -o requirements-dev.lock
```

## Running against your own stack

The broker fails fast unless these are set (see `.env.example` for the full
reference including contract pins and timing knobs):

| Variable | Meaning |
|---|---|
| `HUB_ISSUER` / `HUB_JWKS_URI` | The workforce IdP the broker re-validates hub JWTs against |
| `BROKER_PUBLIC_URL` | Public base URL for consent redirects |
| `VAULT_ADDR` + `VAULT_TOKEN`(`_FILE`) | OpenBao / Vault KV-v2 custody backend |
| `REGISTRY_PATH` | The vendor registry JSON (see below) |
| `COORD_BACKEND` | `memory` (single replica only) or `redis` (+`REDIS_URL`) |

Provision custody per `docs/operations.md` (two KV-v2 mounts, the
`deploy/openbao-policy.hcl` ACL, a scoped token — never root). Copy
`registry.example.json`, edit, and validate against
`schemas/vendor-registry.schema.json` — registry changes are reviewed
changes; the scope ceiling is a security boundary.

## Wire contract

An existing gateway plugin (e.g. Kong `vendor-token`) can point at this
broker unchanged. Frozen surface: resolve status codes 200/404/409/5xx;
response fields `access_token`, `authorize_uri`, `missing_scopes`; problem
`title` slugs; audit event names. `tests/integration/test_wire_compat.py`
asserts all of it on every CI run.

## Documentation

Published site (all three docs, rendered diagrams):
**https://chief-builder.github.io/vendor-token-broker-docs/**
(regenerate with `python tools/build-pages.py`, then push `docs/index.html`
to the public `vendor-token-broker-docs` repo)

- `docs/design.md` — the normative design (API, state machine, refresh
  race, failure modes, security requirements)
- `docs/operations.md` — custody provisioning, config reference, gateway
  contract, runbook
- `docs/adr/0001-redis-coordination.md` — why Redis for multi-replica
  coordination
- `docs/token-lifecycle.md` — the full token lifecycle as sequence
  diagrams: consent, cache, single-flight refresh, multi-replica takeover,
  scope step-up, STALE, revocation, outages
- `docs/smoke-tests.md` — illustrated manual walkthrough of every security
  property (curl + browser), with sequence diagrams

## Provenance

Extracted and hardened from the Vendor Token Broker of the internal
`mcp-healthcare-reference` lab, commit `ab699f3a46bb18ab96cb9d17f3cb9e883e6011c6`.
