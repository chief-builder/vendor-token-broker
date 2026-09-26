# vendor-token-broker

OAuth credential custodian for third-party SaaS vendors: acquires,
custodies, refreshes, and revokes vendor tokens per enterprise user, and
resolves them per-request for an egress gateway.

**Custodian, not issuer.** The broker issues no access tokens and exposes no
token-minting or JWKS endpoint — the route table is audited for exactly that
on every push (`tests/unit/test_routes.py`). Vendor client-authentication
assertions (`private_key_jwt`) may be signed with keys from custody.

## What it does

- **Resolve** (`POST /v1/tokens/resolve`): the egress gateway presents the
  caller's hub JWT (independently re-validated: algorithms pinned to `HUB_ALGORITHMS` —
  default PS256/ES256, RS256 and HMAC never allowed — issuer,
  exactly one tier audience, contract version) and gets back a live vendor
  access token — refreshed inline when needed, targeting `min_ttl_s` with clamping and short-lived-token exceptions
  (see [API reference](docs/api.md#resolve-a-token)).
- **Consent** (`/v1/authorize/{vendor}` → hub sign-in → `/v1/callback/_hub`
  → vendor AS → `/v1/callback/{vendor}`): the browser must sign in at the hub
  as the user the link was issued to, and the same browser (binding cookie)
  must finish both legs. PKCE S256 on both legs, single-use `state`, RFC 9207
  `iss` validation including the omission case, scopes capped by the registry
  ceiling.
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

.venv/bin/pytest tests/unit tests/integration -m "not external and not multi and not gateway"
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

The broker fails fast unless the required variables below are set (see
`.env.example` and the configuration reference in `docs/operations.md` for
contract pins and timing knobs):

| Variable | Meaning |
|---|---|
| `HUB_ISSUER` / `HUB_JWKS_URI` | The workforce IdP the broker re-validates hub JWTs against |
| `BROKER_PUBLIC_URL` | Public base URL for consent redirects |
| `VAULT_ADDR` + `VAULT_TOKEN`(`_FILE`) | OpenBao / Vault KV-v2 custody backend |
| `REGISTRY_PATH` | The vendor registry JSON (see below) |
| `HUB_LOGIN_CLIENT_ID` | The broker's OIDC client at the hub (`HUB_LOGIN_CLIENT_SECRET` optional; public client if omitted): users sign in there before linking a vendor account (`docs/operations.md`) |

Optional but important: `COORD_BACKEND` — `memory` (default, single replica
only) or `redis` (+`REDIS_URL`) for more than one replica.

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

Published site: **https://chief-builder.github.io/vendor-token-broker-docs/**

- [Overview](docs/overview.md) and [quickstart](docs/quickstart.md)
- [Integrate with MCP](docs/mcp-integration.md)
- [API reference](docs/api.md)
- [Deploy and operate](docs/operations.md)
- [Security and MCP alignment](docs/security.md)
- [Design](docs/design.md), [token lifecycle](docs/token-lifecycle.md),
  [manual verification](docs/smoke-tests.md), and [Redis ADR](docs/adr/0001-redis-coordination.md)

Build with `.venv/bin/python tools/build-pages.py`; verify with
`.venv/bin/python tools/build-pages.py --check`. See
[Maintaining the docs](docs/maintaining.md) for validation and publishing
the allowlisted static artifact to the existing public companion repo.

## Provenance

Extracted and hardened from the Vendor Token Broker of the internal
`mcp-healthcare-reference` lab, commit `ab699f3a46bb18ab96cb9d17f3cb9e883e6011c6`.
