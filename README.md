# vendor-token-broker

Let Claude Code and other AI assistants use GitHub as each signed-in person,
without anyone handling tokens.

- **MCP gateway** (`src/mcp_gateway/`). Your assistant connects to it like
  any MCP server. It calls GitHub's official MCP server as the person who is
  signed in, with read-only tools by default. If their GitHub account isn't
  connected yet, the assistant shows a link to connect it.
  ([docs](docs/mcp-gateway.md))
- **Token broker** (`src/token_broker/`). It keeps each person's vendor
  tokens (GitHub and others) in OpenBao or Vault, refreshes them before they
  expire, and cancels them at the vendor when the person disconnects. The
  gateway asks it for a token on every call.

Neither part ever shows a token to the assistant or writes one to a log.

**The broker keeps tokens; it never creates them.** It has no endpoint that
issues access tokens or publishes signing keys, and a test checks its seven
routes on every push (`tests/unit/test_routes.py`). It may sign login
requests to vendors (`private_key_jwt`) with keys from storage.

## How the broker behaves

- **Hands out tokens** (`POST /v1/tokens/resolve`). The gateway sends an
  internal sign-in token (the "hub JWT"). The broker checks it again itself:
  only the algorithms in `HUB_ALGORITHMS` (default PS256/ES256, never RS256
  or HMAC), the issuer, exactly one tier audience, and the contract version.
  It returns a live vendor token, refreshing it first if needed. It aims for
  `min_ttl_s` of remaining life, with limits and exceptions for vendors whose
  tokens are short-lived ([details](docs/api.md#resolve-a-token)).
- **Connects accounts** (`/v1/authorize/{vendor}` → sign in at the hub →
  `/v1/callback/_hub` → vendor → `/v1/callback/{vendor}`). The person must
  sign in as the user the link was made for. The same browser must finish
  every step (a cookie ties them together). Both steps use PKCE, each link
  works once, the vendor's `iss` is checked (RFC 9207, including when it is
  missing), and scopes are capped by the vendor registry.
- **Keeps tokens fresh safely.** Only one request refreshes a person's token
  at a time. A compare-and-swap check stops an older token from overwriting a
  newer one. A connection that can't be refreshed any more becomes `STALE`,
  and the person must reconnect. If many go stale at once at one vendor, the
  broker raises an alert.
- **Disconnects cleanly.** It cancels the token at the vendor first
  (RFC 7009), then deletes its copy.
- **Fails safe.** If token storage is down, it serves only what is in its
  short memory cache (`CACHE_TTL_S`, default 60 s). After that it answers
  503. It never treats "storage down" as "not connected", and never hands out
  a token it couldn't check.
- **Signs in to vendors** with `client_secret_post`, `client_secret_basic`,
  or `private_key_jwt` (RFC 7523), set per vendor in the registry.

## Quickstart

You need Docker and Python 3.12. The [Quickstart guide](docs/quickstart.md)
walks through signing in and using GitHub tools from Claude Code. Setup and
tests:

```sh
git clone <this-repo> vendor-token-broker && cd vendor-token-broker
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-deps -e .

# Everything for the gateway: Keycloak, the gateway, a broker, a GitHub
# stand-in, plus the broker test stack (OpenBao, Redis, test vendor, hub stub).
docker compose -f tests/stack/docker-compose.yml --profile gateway up -d --build --wait

.venv/bin/pytest tests/unit tests/integration -m "not external and not multi and not gateway"
.venv/bin/pytest tests/integration -m "gateway and not external"
```

Two brokers sharing work through Redis, behind a load balancer:

```sh
docker compose -f tests/stack/docker-compose.yml --profile multi up -d --build --wait
BROKER_URL=http://localhost:8400 BROKER_CONTAINERS=vtb-broker-a,vtb-broker-b \
  .venv/bin/pytest tests/integration/test_multi_replica.py
```

## Dependencies

Installs are hash-pinned: `requirements.lock` (runtime + redis, used by the
broker image), `requirements-gateway.lock` (the gateway image), and
`requirements-dev.lock` (adds the dev tools and the gateway, used by CI).
All three are generated from `pyproject.toml` and work on every platform. After
changing a dependency in `pyproject.toml`, regenerate them:

```sh
uv pip compile pyproject.toml --extra redis --universal --python-version 3.12 \
  --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra redis --extra dev --extra gateway --universal \
  --python-version 3.12 --generate-hashes -o requirements-dev.lock
uv pip compile pyproject.toml --extra gateway --universal --python-version 3.12 \
  --generate-hashes -o requirements-gateway.lock
```

The gateway image uses `requirements-gateway.lock`, so FastMCP never enters
the broker image.

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

- [Overview](docs/overview.md) and [Quickstart](docs/quickstart.md)
- [MCP gateway](docs/mcp-gateway.md)
- [Connect your own MCP server](docs/mcp-integration.md) and the
  [broker API](docs/api.md#resolve-a-token)
- [Deploy and operate](docs/operations.md) and [Security](docs/security.md)
- [Design](docs/design.md), [token lifecycle](docs/token-lifecycle.md),
  [smoke tests](docs/smoke-tests.md), and the [Redis decision](docs/adr/0001-redis-coordination.md)

Build with `.venv/bin/python tools/build-pages.py`; verify with
`.venv/bin/python tools/build-pages.py --check`. See
[Maintaining the docs](docs/maintaining.md) for validation and publishing
the allowlisted static artifact to the existing public companion repo.

## Provenance

Extracted and hardened from the Vendor Token Broker of the internal
`mcp-healthcare-reference` lab, commit `ab699f3a46bb18ab96cb9d17f3cb9e883e6011c6`.
