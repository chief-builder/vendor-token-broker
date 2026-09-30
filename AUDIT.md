# Repository audit — 2026-09-30

Scope: the whole repository at `origin/main` `524ee8b`, plus the GitHub repo
metadata and the published docs site. The audit changed nothing except adding
this file on branch `hardening/2026-09-30`. The live `vtb-*` Docker stack on
the audit machine was left running, so **the integration suites were not run
locally**. Their evidence comes from CI (the latest `main` runs on 2026-09-30
all passed), plus test collection and code reading.

Status legend: **Verified** (with evidence), **Wrong**, **Unverifiable** (the
code, tests, or a command cannot confirm it).

---

## 1. What the project does (from the code)

`src/token_broker` is a FastAPI service with exactly seven routes. It holds
per-user OAuth tokens for third-party SaaS vendors in OpenBao/Vault KV-v2 and
hands a live access token to a caller that presents a re-validated hub JWT. It
also runs the browser consent flow (hub OIDC login, then the vendor
authorization code with PKCE) and refreshes tokens single-flight with a KV-v2
compare-and-swap. Revocation goes to the vendor first, then custody.
`src/mcp_gateway` is a separate FastMCP service. It authenticates MCP clients
against the hub, exchanges their token for a hub JWT (RFC 8693), and gets the
user's vendor token from the broker. It then forwards allowlisted,
read-oriented tool calls to the GitHub, Linear, Atlassian, and Cloudflare MCP
servers, and returns a consent link (URL elicitation) when an account is not
connected.

---

## 2. Accuracy

Two agents and I checked every factual claim in README.md, `docs/*.md`,
CHANGELOG.md, CLAUDE.md, `.env.example`, `deploy/`, the repo description, and
the published site against code, tests, and commands. About 300 claims were
checked. Most are **Verified**, many with file:line evidence, and the
walkthroughs match the code closely. Everything that is not Verified is listed
below. A condensed table of the Verified claims is in
[Appendix A](#appendix-a--verified-claims-condensed).

### 2.1 Wrong

| # | Where | Claim | What is true | Evidence |
|---|---|---|---|---|
| W1 | README.md:163 | "the **internal** `mcp-healthcare-reference` lab" | That lab is a **public** repo on this account. "Internal" reads as a private or employer project. | `gh repo view chief-builder/mcp-healthcare-reference` → `PUBLIC` |
| W2 | docs/security.md:20 | "Source and test paths refer to the private repository available to maintainers." | This repo is public | `gh repo view` → `PUBLIC` |
| W3 | docs/maintaining.md:45 | "The source repository is private." | Public | same |
| W4 | CHANGELOG.md:136-138 | "393 unit tests and 108 integration tests (61 per backend, 7 multi, 37 gateway, 3 external)" | 433 unit (all pass). 123 integration: 64 per backend, 7 multi, 51 gateway-marked (47 + 4 also external), 5 external. | `pytest --collect-only -q` per marker; `pytest tests/unit -q` → 433 passed |
| W5 | docs/mcp-gateway.md:423 | "The broker doesn't have this gap, because it never caches single keys." | The per-kid cache is off (`hub.py:34-37`), but `PyJWKClient` caches the whole JWK set for 300 s, so a removed hub key stays trusted for up to about 5 minutes | PyJWT `PyJWKClient(lifespan=300)` default |
| W6 | deploy/docker-compose.yml:3, docker-compose.multi.yml | "Configure via .env (see ../.env.example for the full reference)" | Only the listed `environment:` keys reach the container. `GITHUB_/LINEAR_/ATLASSIAN_/CLOUDFLARE_CLIENT_ID` (the `enabled_env` gates), `HUB_LOGIN_HINT`, `HUB_DISCOVERY_URL`, and every timing knob are dropped. Real vendors come up disabled (404 `unknown-vendor`), and a real IdP needs `HUB_LOGIN_HINT=none` (operations.md:319). | `deploy/*.yml`, `vendors.py:69-71` |
| W7 | docs/mcp-gateway.md:287 | A snapshot value containing `/` is relative to the upstreams file | True only for a custom `GATEWAY_UPSTREAMS` file. The bundled file resolves under `snapshots/`. | `mcp_gateway/config.py:67-70` |
| W8 | docs/design.md:786 | Link `token-lifecycle.md#5-multi-replica-refresh-persisted-…` | The anchor is broken on GitHub, because the em dash makes the slug `refresh--persisted`. It works on the generated site. | GitHub slug rules; `build-pages.py --check` passes |
| W9 | docs/operations.md:51 | `-ttl=768h` as an example of a hard maximum TTL | `-ttl` sets the initial TTL. The cap is `-explicit-max-ttl` or the mount/system max. | OpenBao token docs |
| W10 | CLAUDE.md (invariants) | A redis outage gives 503 `coordination-unavailable` on "authorize/callbacks" | Authorize does. The browser callbacks return an HTML 503 page with no problem title (design.md and the ADR say so correctly). | `main.py:501-504, 615-618` |
| W11 | docs/design.md:3 | "Reviewed 2026-09-27" | `site.json` and security.md say 2026-09-28 | files |
| W12 | docs/design.md §11 | `broker.resolve` fields are `decision` + `path` … | Incomplete: the `invalid_request`/`sub_mismatch` denials use `reason` and `claimed_sub`, and the scope-ceiling denial adds `required` and `ceiling` | `main.py:250, 256, 277` |
| W13 | docs/overview.md:69 | "A test searches every container's logs for all of them" | The Cloudflare stand-in token is not in the searched set | `test_mcp_gateway.py:357-371` |
| W14 | CHANGELOG.md (Security) | "custody keeps 2 versions per entry" | This is an operator setting (`max_versions=2` in provisioning), not code behavior | `openbao-init/init.sh`, operations.md |
| W15 | docs-site-review.md (tracked at the repo root) | Whole file | Stale working notes dated 2026-09-25: 319 unit tests, "no gateway", Mermaid `@11`, `/tmp` evidence paths, and a "private-repository link". maintaining.md:41 still points readers to it. | file contents |

Minor imprecisions: smoke-tests.md says "issuer recorded when the link was
made", but the issuer is recorded when the vendor state is created after the
hub login (`main.py:550`). operations.md says the private-key format is
"PEM, PKCS8", but PKCS1 also works. mcp-gateway.md:146-148 leaves Cloudflare
out of the snapshot-refresh examples.

### 2.2 Unverifiable (need a test or script, or a rewording)

| # | Claim (where) | Why it can't be verified here | Proposed handling |
|---|---|---|---|
| U1 | "Verified end to end with Claude Code 2.1.283" (CHANGELOG, overview, mcp-gateway, security) | Manual test against a third-party client | Reword to "manually tested with Claude Code 2.1.283 on <date>" and keep it in the Verification record |
| U2 | Real-service behavior: GitHub/Linear stop at MCP 2025-11-25; Linear's full server refuses read-only tokens; Linear ATs live 24 h after RT revoke; Keycloak ignores `resource`; Okta/Entra RS256 or on-behalf-of; Figma refused registration; Sentry/GitLab/… accept plain OAuth tokens | Vendor behavior, not in the repo | Mark as "observed on <date>" (security.md already has a verification record, so point to it). `tests/integration/test_external_mcp_servers.py` covers part of this when credentials are present. |
| U3 | Secret expiry dates 2026-12-27 / 2026-12-26 and "about 90 days" | Held in the user's `.env` or the vendor console | Keep, labelled as an operator note, not a code fact |
| U4 | "An existing gateway plugin (e.g. Kong `vendor-token`) can point at this broker unchanged" | The plugin is in the source lab, not here | Reword: "the wire contract frozen by `test_wire_compat.py` is the one the source lab's Kong plugin parses", with a link to the public lab |
| U5 | "never hands out a token it couldn't check" (README) | Vague | Reword to the concrete rule: custody down → cache (≤ `CACHE_TTL_S`) or 503 |
| U6 | `uv pip compile …` regeneration commands (README) | Not run during the audit | Run them in Phase 2 when re-locking; this verifies them |
| U7 | Registry `atlassian.auth_metadata_url` contains an opaque ID (`VCeDsk8Z…`) | Probably Atlassian's global MCP AS ID, but could be tenant-specific | Please confirm; if it is tenant-specific, replace it with a placeholder |
| U8 | design.md §12 SLOs; ADR lab history | Targets and history, labelled as such | Leave as is |
| U9 | GitHub "Read-only App permissions", Atlassian admin domain setting, Claude Code `--callback-port` | Vendor or product UI | Leave, as setup instructions |

### 2.3 Repo metadata, site, links, paths, employer references

- **Description:** accurate ("Jira" is served through Atlassian, together with
  Confluence). **Homepage URL is empty**; recommend the docs site.
- **Published site** (https://chief-builder.github.io/vendor-token-broker-docs/):
  HTTP 200 and byte-identical to `docs/index.html`, so it is current. It is
  published by hand-copying into a second public repo.
- **Links:** all relative links in the docs resolve, except W8 on GitHub. All
  external links return 200; the vendor MCP endpoints return 401 as expected.
  CI has no link check.
- **Absolute local paths:** none in tracked files (`git grep '/Users/|/home/|/private/tmp'` → 0).
- **Employer references:** none found. There are no company names, corporate
  domains, or non-noreply emails, and every commit is by `chief-builder` with
  a personal or noreply address. The only item to fix is the wording in W1.
  Neutral mentions such as "source lab" and `mcp://tier/internal` are generic.

---

## 3. Currency (checked 2026-09-30 against PyPI, python.org, Docker Hub, GitHub releases, modelcontextprotocol.io)

### Runtime

| Item | Repo | Latest stable | Note |
|---|---|---|---|
| Python | 3.12 (`requires-python >=3.12`, Dockerfiles `python:3.12-slim@sha256:2f17…`, CI "3.12") | **3.14.7** (bugfix); 3.13.15 (bugfix); 3.12.14 is **security-only** (EOL 2028-10); 3.15.0 is due 2026-10-01 | All 433 unit tests pass on 3.14.7 (clean clone). No `.python-version` file. |
| Base image digest | `2f17fc04…` (3.12.14) | the tag now points at `f77ac9e4…` (same Python, newer OS layer) | `tests/stack/hub-stub` and `mock-vendor` use `python:3.12-slim` with no digest and install unpinned `fastapi==0.115.*` etc. |

### Python dependencies (locked → latest)

Everything is current except the entries below. **pip-audit 2.10.1 reports no
known vulnerabilities in any of the three locks** (PyPI and OSV services).

| Package | Locked | Latest | Upgrade risk |
|---|---|---|---|
| PyJWT | 2.14.0 (runtime/dev) vs **2.15.0 (gateway)** | 2.15.1 | Safe. 2.15.0 hardens deeply nested input. **The locks disagree.** |
| uvicorn | 0.53.0 (runtime/dev) vs **0.54.0 (gateway)** | 0.54.0 | Safe (the only additions are opt-in HTTP/2). **The locks disagree.** |
| fastapi | 0.141.1 | 0.142.2 | Safe (adds opt-in OpenTelemetry) |
| ruff | 0.16.8 | 0.16.9 | Safe |
| markdown | 3.10.3 | 3.11 | Safe (drops Python 3.10) |
| sse-starlette (via fastmcp) | 3.4.11 | 3.5.0 | Leave to fastmcp's resolver |
| fastmcp | 4.0.10 | 4.0.10 | Current |
| mcp SDK | 2.2.0 | 2.2.0 | Current |
| redis-py | 8.1.0 | 8.1.0 | Current. The code calls the deprecated `setex` (`coordination.py:338`; 4 warnings in the unit run). |

### Container images (tests/stack, deploy)

| Image | Pinned | Latest | Note |
|---|---|---|---|
| `openbao/openbao:2` | floating | 2.7.0 | Silently moved to 2.7.0; pin it |
| `redis:7-alpine` | floating (7.4.11) | 8.10.2 | 7.4 is still patched; moving to 8 is a major bump that needs the multi-replica suite. **Hold.** |
| `nginx:1.27-alpine` | 1.27 | 1.30.5 stable | The 1.27 tag has not been rebuilt since 2025-04-16. Bump. |
| `quay.io/keycloak/keycloak:26.7.4` | 26.7.4 | 26.7.4 | Current |
| `curlimages/curl:8.10.1` | 8.10.1 (2024) | 8.22.0 | Bump |

None of the compose images is pinned by digest.

### GitHub Actions

| Action | Pinned | Latest | Note |
|---|---|---|---|
| actions/checkout | v4.4.0 (SHA) | v7.0.1 `3d3c42e5…` | **v4 targets Node 20, which GitHub removed from runners on 2026-09-23.** Today's run is forced onto Node 24 with a warning. The v7 breaking changes (ESM, `pull_request_target` fork guard) do not affect this workflow. |
| actions/setup-python | v5.6.0 (SHA) | v7.0.0 `5fda3b95…` | Same Node 20 issue. v7 removes the `pip-install` input, which is not used here. |

Also: `ubuntu-latest` moves to Ubuntu 26 from 2026-10-19 (notice on the current runs).

### MCP specification

- **Latest published revision: 2026-07-28** (modelcontextprotocol.io/specification/versioning;
  `/specification/latest` redirects there). The draft has no announced changes.
- **The repo targets 2026-07-28**: `MRTR_ERA`, MRTR `InputRequiredResult` URL
  elicitation, and a 2025-11-25 fallback with in-call elicitation. Docs link
  2026-07-28 pages. **This matches the latest revision.**
- Gaps, all documented already or partly:
  1. **`subscriptions/listen`**: the mcp SDK 2.2.0 implements it, FastMCP
     4.0.10 does not (PrefectHQ/fastmcp#4920 is open). CLAUDE.md's claim is
     accurate, and the snapshot workaround is still needed.
  2. **Upstreams run the `legacy` handshake**, because GitHub and Linear
     negotiate at most 2025-11-25 (a vendor limit).
  3. **The gateway's own client does not use 2026-07-28 `server/discover` or
     per-request `_meta` version negotiation.** Not needed today; worth one
     line in the docs.
  4. **CIMD** (Client ID Metadata Documents; 2026-07-28 deprecates DCR in
     favor of it): the broker registers with vendor servers through DCR
     (`tools/register-mcp-client.py`). Documented in mcp-integration.md.

---

## 4. Design

Overall the design is sound and deliberate. Protocols (`Custody`,
`Coordination`) make the backends swappable. `Broker(cfg, custody=…, hub=…,
vendors=…, coord=…)` and `Gateway(cfg, hub, broker, routes, current_token)`
already use constructor injection, which is how the offline unit harness
drives the real app. Configuration is two fail-fast frozen dataclasses. Errors
map to deliberate problem titles, and exception messages that could carry
hostnames are replaced with fixed text. Findings:

| # | Finding | Where | Severity | Proposal |
|---|---|---|---|---|
| D1 | `main.py` is 814 lines. The route closures hold the resolve waiter logic, both consent callbacks, and revoke/park. It is readable, but it is the one module without a clear single responsibility. | `main.py:243-812` | Medium | **Proposal only (redesign):** move the consent legs to `consent.py` and revoke/park to `grants.py`, with routes calling into them. Behavior-neutral, but a large diff on security-critical code, so I would not do it without your go-ahead. |
| D2 | Not all configuration goes through `Config`: `VendorClient.get_vendor` reads `os.environ[enabled_env]` on every call | `vendors.py:69-71` | Low | Resolve the enabled set once at startup (from `Config`/env) and inject it. This makes the deploy-compose gap (W6) visible at startup. |
| D3 | A hard-coded value that belongs in config: the GitHub revocation URL `https://api.github.com/applications/{id}/grant` | `vendors.py:209` | Low | Add an optional `revocation.url` to the registry schema, defaulting to the current URL. This also enables GitHub Enterprise. |
| D4 | `hub_login` reads a private attribute of `HubValidator` (`self._validator._jwks`), and the JWKS error handling is duplicated with `hub.py` | `hub_login.py:107` | Low | Give `HubValidator` a public `signing_key(token)` method used by both |
| D5 | Gateway error handling: `rejected = "401" in str(exc)` is a string heuristic; `Hub.exchange` indexes `r.json()["access_token"]` unguarded (a 200 with no token or non-JSON raises an unmapped error); the shared `httpx.AsyncClient` is never closed | `server.py:245`, `clients.py:45`, `server.py:383` | Low–Med | Check `httpx.HTTPStatusError.response.status_code`; map a malformed hub response to `HandoffError`; close the client in a lifespan hook |
| D6 | Two audit implementations: the broker `print`s JSON lines, the gateway goes through `logging`. The broker's `log.debug` diagnostics are never configured, so they are invisible. | `audit.py`, `server.py:55` | Low | Keep both formats (event names are frozen). Document that audit lines go to stdout. Optionally route the broker's audit through a `logging` logger with a JSON formatter so operators control levels. |
| D7 | Duplicated constant: `ALLOWED_HUB_ALGORITHMS` is defined in both packages (intentional, since the gateway image must not import the broker) | both `config.py` | Info | Add a unit test asserting the two sets are equal |
| D8 | Dead or test-only code: `Coordination.get_txn` is used only by tests; `profile` attributes are unused; `tools/build-pages.py:60 section_id` and `tools/mcp-demo-client.py:25 response_type` are unused variables | vulture | Low | Remove the unused variables; keep `get_txn` (the harness uses it) but say so in its docstring |
| D9 | The vendor metadata cache never expires (process lifetime) | `vendors.py:87-100` | Low | Document it, or give it a TTL (a restart is the current refresh path) |
| D10 | 49 mypy errors (pyright: 71). Most are `Optional` narrowing mypy cannot follow; a few are real loose types (`dict[str,str]` receiving a list, `refresh.py:156`). No type checking in CI. | mypy run | Medium | Fix the errors, then add mypy to CI |
| D11 | Code is not formatted by the formatter the repo already ships: `ruff format --check` → 68 of 70 files would change | ruff | Low | A single `style:` commit plus `.git-blame-ignore-revs`, then enforce in CI |
| D12 | Deprecated API: redis-py `setex` | `coordination.py:338` | Low | `set(key, value, ex=ttl)` |
| D13 | FastMCP/pydantic-settings loads `.env` from the current directory when `fastmcp` is imported (seen by an audit agent: parse warnings from the repo's root `.env`). This is implicit environment loading outside `GatewayConfig`. | fastmcp settings | Low | Document it. In the image the working directory has no `.env`. |
| D14 | Registry schema gaps: `revocation.type` is an unconstrained string (a typo silently becomes RFC 7009); `vendor_id` is not checked against the key | `schemas/vendor-registry.schema.json:76` | Low | Add `enum: ["rfc7009", "github_grant"]` |

---

## 5. Tests

| Suite | Count | Result | How run |
|---|---|---|---|
| Unit (offline harness over the real app) | 433 | **433 passed** in about 19 s on Python 3.12.14 and 3.14.7, from a clean clone | `pytest tests/unit -q` |
| Integration, memory and redis | 64 each | Passed in CI on `main` 2026-09-30 (not run locally, since the live stack is in use) | `ci.yml` integration matrix |
| Integration, multi-replica | 7 | Passed in CI | `ci.yml` multi |
| Integration, gateway (Keycloak) | 47 (+4 external) | Passed in CI | `ci.yml` gateway |
| External (real vendors) | 5 | Skipped without credentials | `-m external` |

**Unit coverage: 91%** (2026 statements, 181 missed; `pytest-cov` over
`token_broker` and `mcp_gateway`). By module: hub, client_auth, config, audit,
and problems 100%; lifecycle 99%; custody 96%; refresh 94%; gateway server
94%; hub_login and vendors 93%; coordination 89%; main 88%; sweeper 83%;
upstream 76%; **gateway clients 65%**. Coverage is not measured in CI.

**Most important untested paths** (unit level; some are covered by
integration tests, noted in brackets):

1. **Gateway `clients.py`:** `Hub.exchange` (the RFC 8693 handoff: 5xx →
   Unavailable, 4xx → HandoffError) has no unit test at all, and neither does
   `Broker.resolve`'s status mapping (200/404/409/401/5xx). This is
   security-relevant and currently exercised only end to end.
2. **Vendor callback rejections:** RFC 9207 `iss` missing-when-required and
   mismatch (`main.py:597-601`) [integration `test_consent.py:97`]; the state
   consumption race (612-614); the vendor `error=` constant page (622-624).
3. **Fail closed on redis** in authorize, both callbacks, and DELETE
   (`main.py:440, 467, 501, 557, 615, 672, 710`). This is an invariant in
   CLAUDE.md with no unit test for the consent paths.
4. **DELETE self-service `sub` mismatch → 403** (`main.py:703`)
   [integration `test_grants.py`]. It needs a unit negative test.
5. **`GET /v1/grants` success path**, including hiding REFRESHING
   (`main.py:779-796`).
6. **GitHub `github_grant` revocation** (`vendors.py:203-214`): entirely
   untested.
7. Refresh: CAS lost while writing the REFRESHING marker and while restoring
   ACTIVE (`refresh.py:119-121, 134-135`).
8. Hub login: ID token when the JWKS is unavailable or the key is bad
   (`hub_login.py:108-111`).

Clean-clone "one command": there is none. Unit tests need the venv steps, and
integration needs the compose stack (see §8).

---

## 6. CI/CD

`.github/workflows/ci.yml` (on every `push` and `pull_request`) has these jobs:
lint-and-schema (ruff check, registry schema, docs generator `--check`); unit;
docker-build (both images); integration matrix (memory, redis); gateway;
multi. Actions are SHA-pinned. It is green on `main`. There are no other
workflows; the docs site is copied by hand to `vendor-token-broker-docs`.

Gaps:

| Gap | Impact |
|---|---|
| Pinned actions are on Node 20, which runners removed on 2026-09-23 | Works only through the forced Node 24 fallback |
| No top-level `permissions:` (the repo default happens to be `read`) | Least privilege depends on a repo setting, not the file |
| No `concurrency` group; no `timeout-minutes` on any job | Superseded runs keep burning minutes, and a hung compose stack runs to the 6 h limit |
| `push` on all branches **plus** `pull_request`, so every PR commit runs the whole stack twice | Doubles cost (restrict `push` to `main`) |
| No format check, type check, coverage, wheel/sdist build, or link check | Requested standards not enforced |
| No CodeQL, no Dependabot config, no dependency review | Security currency relies on manual effort |
| No pip cache | Slower jobs |
| Docs published by hand to a second repo | Can drift from `main`. It was current on 2026-09-30, but nothing enforces that. |

---

## 7. Security

**Good:**
- Hash-pinned locks, digest-pinned base images, non-root images (UID 65534).
- Exactly 7 routes, with no OpenAPI or docs routes.
- Algorithm allowlist (no RS256, HMAC, or `none`).
- One tier audience and a contract pin.
- PKCE + nonce + a single-use state bound to the user and browser.
- RFC 9207 checks on the vendor leg.
- Self-service DELETE checks `sub`.
- The admin endpoint requires a list-typed `groups` claim and strips keys containing `secret`.
- Custody failure maps to 503, never to "absent".
- Tests grep container logs for token material.
- Vendor error text is not reflected into browser pages.

**Checked:**

| Area | Result |
|---|---|
| Dependency vulnerabilities | `pip-audit` on all three locks: **0 known vulnerabilities** (PyPI and OSV) |
| Secrets in git history | `gitleaks git` over all 58 commits: 3 hits. Two are false positives (`on_duplicate="replace"`). One is the intentionally committed test key `tests/stack/keys/mockhub-jwt-private.pem`, which is labelled (`tests/stack/keys/README.md`, `.github/secret_scanning.yml`). **No real secrets.** |
| Test credentials | Compose and realm carry dev-only values (`root`, `mock-secret`, `hub-login-secret`, `gateway-dev-secret`, admin/admin). They are only for the local stack but not uniformly labelled as test-only. |
| Local `.env` | Git-ignored and never committed. **Disclosure:** during the audit I displayed the local root `.env` in my tool output. It holds a GitHub App client ID and **client secret**. It went nowhere except this local session transcript, but you may want to rotate that secret. |
| Repo security settings | Secret scanning, push protection, Dependabot alerts and security updates are **all disabled**. CodeQL is **not configured**. `main` is **not protected**. `.github/secret_scanning.yml` has no effect while scanning is off. |
| Workflows | Default token is `read` (repo setting) but not declared in the file. No `pull_request_target`. No secrets used. |
| Input validation | Resolve body validated (`parse_resolve_body`). Vendor IDs are regex-checked at load (path-safe). Subjects are base64-encoded in custody paths. `min_ttl_s` is capped. A missing `txn` query gives FastAPI's default 422 JSON rather than problem+json (documented). |
| Authn/authz | Hub JWT re-validated on every route that needs it. The gateway checks MCP token audience, issuer, algorithm and scope, and audits refusals. Known gaps are documented in security.md "Known limitations": the hub callback does not reject a missing `iss`, and vendor discovery does not pin the expected issuer. The JWK-set cache is 300 s at the broker (W5) and 1 h at the gateway. |
| Unsafe defaults | `BROKER_PUBLIC_URL` over http makes the binding cookie non-Secure; that is intended for localhost only. The deploy compose publishes `8300:8300` plain HTTP with no note about terminating TLS in front of it. |
| Deploy token file | The image runs as UID 65534 and the compose secret is a bind mount. A `0600` token file owned by the host user is unreadable, so startup fails with "VAULT_TOKEN_FILE unreadable". Not documented or tested. |
| `.gitignore` gaps | `.DS_Store`, `.env.*` (with `!.env.example`), `.coverage*`, `htmlcov/`, `coverage.xml`, `.mypy_cache/`, `.idea/`, `.vscode/`, `*.swp`, `build/`. Also untracked local review files at the root (`review*.md`, `fix_plan.md`). |

No high or critical findings.

---

## 8. Onboarding (README quickstart from a clean clone of `main` @ `524ee8b`)

1. `git clone <this-repo> …`: the URL is a placeholder.
2. `python3.12 -m venv .venv` → `command not found: python3.12` on a machine
   that has only 3.14. The README says "Python 3.12" while pyproject says
   `>=3.12`, and 3.14 works.
3. The obvious fallback `uv venv --python 3.12 .venv` creates a venv **without
   pip**, so `.venv/bin/pip` → "no such file or directory". It needs
   `uv venv --seed`.
4. After that, both installs succeed (the lock installs in about 8 s).
5. The README lists no required host ports. The gateway profile binds
   8180, 8210, 8211, 6390, 8300, 8310, 8320, 8330, 8500, 8600 (multi adds
   8400-8402). The README also gives no Docker memory guidance: Keycloak needs
   about 1-1.5 GB, and Colima's default VM is 2 GiB.
6. **Hazard:** the compose project name comes from the directory
   (`tests/stack`), so every clone is project `stack`, and container names are
   fixed. Running `up` from a second clone silently takes over an existing
   stack. Set a top-level `name:`.
7. `.env` guidance is confusing. `.env.example` is at the root and describes
   the broker, but the stack reads `tests/stack/.env`. The stack needs no env
   file at all, and the README doesn't say so.
8. The README brings up `--profile gateway` and then runs the non-gateway
   integration tests. That works because the stub-hub broker has no profile,
   but it is not explained.
9. The multi-replica step says "let its sweep lease expire (up to 120 s)" and
   gives no command to wait on.
10. There is no single command for "run the tests" (unit or full).
11. The README is missing sections the brief asks for: a "why it matters"
    section, an architecture diagram, project status and limitations, license,
    and a CI badge.

---

## Prioritized plan for Phase 2

Each line is one or a few focused conventional commits. Items marked 🔸 need
your decision (see the questions below).

**P0: correctness of what readers see**
1. `docs:` fix W1-W15, reword U1-U5, drop or relocate `docs-site-review.md` 🔸,
   and regenerate `docs/index.html`.
2. `docs:` restructure the README: summary, why it matters, Mermaid
   architecture, a working clean-clone quickstart (real clone URL, 3.12+, `uv`
   path, ports, memory, env file note), configuration table, tests, status and
   limitations, license, CI badge.
3. `fix(deploy):` deploy compose passes `env_file`, the `enabled_env` gates and
   `HUB_LOGIN_HINT`/`HUB_DISCOVERY_URL`; document token-file ownership and TLS
   termination.

**P1: CI and supply chain**
4. `ci:` bump checkout v7.0.1 and setup-python v7.0.0 (SHA plus version
   comment); top-level `permissions: contents: read`; `concurrency`;
   `timeout-minutes`; `push` only on `main`; pip cache.
5. `ci:` add format check, mypy, unit coverage (report plus a floor at the
   current level), `python -m build`, and a lychee link check (docs, README).
6. `ci:` CodeQL workflow (Python and Actions), `.github/dependabot.yml` (pip,
   docker, docker-compose, github-actions).
7. `build:` pin the runtime 🔸, add `.python-version`, re-lock all three locks
   so PyJWT and uvicorn agree (fastapi 0.142.2, PyJWT 2.15.1, uvicorn 0.54.0,
   ruff 0.16.9, markdown 3.11), refresh the base-image digests, digest-pin the
   stub images and compose images, and bump nginx 1.30, curl 8.22 and
   openbao 2.7.0. **Hold redis at 7.4** (reason: major bump; revisit with a
   multi-replica run).

**P2: tests and design fixes (behavior-preserving)**
8. `test:` unit tests with positive and negative cases for the gaps in §5
   (gateway `clients.py`, iss/state/error callbacks, redis fail-closed on
   consent and DELETE, DELETE 403, grants listing, GitHub revocation, refresh
   CAS races, hub-login JWKS errors), plus an algorithm-set parity test.
9. `refactor:` D2, D3, D4, D5, D8, D12, D14 (small, each its own commit).
10. `style:` one `ruff format` commit plus `.git-blame-ignore-revs`; then
    `fix(types):` for the mypy errors.
11. `build:` a single-command test entry point (a `Makefile` with `make test`
    for unit and `make test-all` for stack up plus the suites) 🔸.

**P3: repo hygiene**
12. `SECURITY.md`, `CONTRIBUTING.md`, `.editorconfig`, issue and PR templates,
    `.gitignore` additions, a CHANGELOG entry, and clear labels on test-only
    credentials in compose and realm.
13. The PR description lists the `gh` commands for settings I will not change:
    - enable secret scanning and push protection
    - enable Dependabot alerts and security updates
    - protect `main` (required checks, no force-push)
    - set the homepage URL and topics
    - optionally, Pages from this repo instead of the second repo

**Proposed but not planned (redesign, needs a separate go-ahead):** D1 (split
`main.py` into `consent.py`/`grants.py`); publishing docs through GitHub Pages
from this repo; adopting CIMD for vendor MCP registration once vendors support
it.

---

## Phase 2 outcome (2026-09-30)

Decisions taken: Python 3.14; one formatting commit; delete
`docs-site-review.md`; split `main.py`; add a Makefile. The Atlassian
metadata ID (U7) was checked: Atlassian's public, unauthenticated
protected-resource metadata for `https://mcp.atlassian.com/v2/mcp` names
exactly `https://auth.atlassian.com/VCeDsk8ZHncYF1g234fKtc4lNipbBhu3`, so it
is Atlassian's global MCP authorization server, not tenant-specific. It stays.

| Finding | Status | Commit |
|---|---|---|
| W1-W3, W5, W7-W13, U1, U4, U5, minor imprecisions | Fixed | `18f1ba2`, `e26b1b5` |
| W4, W14 (CHANGELOG) | Fixed | `c032b4f` |
| W6 (deploy env pass-through), token-file ownership (checked on a Linux Docker host: a 0600 file is unreadable by UID 65534, 0444 works), TLS note | Fixed | `0bc0866` |
| W15 `docs-site-review.md` | Deleted (stale working notes with `/tmp` paths and a wrong "private" claim) | `276d2fc` |
| W13 wording vs. extending the test | Docs narrowed to what the test checks; adding a Cloudflare tool call to the leak test was left out rather than written blind | `18f1ba2` |
| Python 3.14, relocked, base digests, stub images on the hashed lock, `.dockerignore` | Done | `1788e00` |
| Compose images pinned (exact version + digest), project name | Done; **redis held at 7.4.11** (major bump, needs a multi-replica run) | `eceda03` |
| D1 split `main.py` | Done, behavior-neutral (statement-level diff checked) | `11654b0` |
| D2 enabled vendors from injectable env | Done | `5acc319` |
| D3 GitHub grant URL in registry; D14 revocation enum | Done (+ first tests of the `github_grant` path) | `f891145` |
| D4 shared signing-key lookup | Done | `ddedf8d` |
| D5 upstream 401 | **Real bug**, fixed: with MCP SDK 2.2.0 a 401 surfaces as a generic `MCPError(-32603)`, so "reconnect" never fired | `7bc1dd1` |
| D5 malformed hub/broker answers | Fixed | `b1cbd49` |
| D5 shared `httpx.AsyncClient` never closed | **Not done**: only matters at process exit; a lifespan hook around FastMCP's app is more change than it is worth | — |
| D6 two audit implementations | **Not changed**: event names and the stdout JSON-line format are frozen wire contract (SIEM joins); documented as is | — |
| D7 allowlist parity test; D8 dead code | Done | `e097245` |
| D9 vendor metadata cache without TTL | **Not done** (restart refreshes it; low risk) | — |
| D10 mypy | Clean; in CI | `bf4c9a4`, `76ced5d` |
| D11 formatting | Done; enforced in CI; blame-ignored | `fa2793e`, `b81f8f0` |
| D12 deprecated `setex` | Done | `c46b5f9` |
| D13 FastMCP `.env` | Documented | `faaee53` |
| JWKS test relied on aliasing (found during the PyJWT upgrade) | Fixed; window explicit (`JWKS_CACHE_S`) and tested both sides | `e95371a` |
| Untested security paths (§5) | Tests added (positive and negative); mutation-checked | `12b57ac`, `b1cbd49`, `7bc1dd1`, `f891145` |
| CI gaps (§6) | Done: Node 24 actions, permissions, concurrency, timeouts, format, mypy, coverage floor 90%, build, pip-audit, lychee, CodeQL, Dependabot; actionlint and zizmor clean | `76ced5d`, `f4485fb` |
| Hygiene | `.gitignore`, `.editorconfig`, SECURITY, CONTRIBUTING, templates, Makefile | `24af3d0`, `5d65ab9`, `76ced5d` |

**Results after Phase 2** (from a clean clone of the branch, `make check`):
507 unit tests pass, **95% line coverage** (was 91%), ruff and mypy clean,
docs generator check passes, lychee reports 0 broken links, `pip-audit`
reports no known vulnerabilities, all five images build on Python 3.14 and
the broker fails fast listing every missing variable.

**Not run locally:** the Docker integration suites (memory, redis, gateway,
multi-replica). Running them replaces the live `vtb-*` stack on the audit
machine, and the session's sandbox refused that step. They run in CI on the
pull request. Result on PR #1 (CI run 36737024437, Python 3.14): integration
memory **64 passed**, integration redis **64 passed**, gateway **47 passed**,
multi-replica **7 passed**; every other job (checks, audit, unit, build,
links, CodeQL for python and actions) green.

**Proposed, not done:** publish the docs site from this repository through
GitHub Pages (a settings change) instead of copying to a second repository;
adopt CIMD for vendor MCP registration once vendors support it; bump Redis
to 8.x after a multi-replica run.

---

## Appendix A — Verified claims (condensed)

| Document | Verified examples (evidence) |
|---|---|
| README | Gateway upstreams and prefixes (`upstreams.json`, `server.py:103`); read-only snapshots (`test_bundled_snapshots_match_their_allowlists_and_are_read_only`); 7 routes tested on every push (`test_routes.py:24-37`, ci `on: push`); hub JWT checks (`hub.py:76-91`, `config.py:22-44`); consent sequence, PKCE, single-use, same browser (`main.py:425-571`); single-flight + CAS + STALE + mass-stale (`coordination.py`, `refresh.py`, `main.py:183-199`); revoke AT then RT, GitHub grant delete (`vendors.py:196-238`); fail-closed custody with 60 s cache (`main.py:147-157, 414-417`); client auth methods (`client_auth.py`); required env vars (`config.py:17-18, 111-123`); wire freeze (`problems.py`, `test_wire_compat.py`); lock files and their image use (Dockerfiles); provenance commit exists (`gh api`); `build-pages.py --check` passes |
| overview.md | Tool counts 7/13/8/3; exchange diagram (`clients.py:28-45`); fresh session per call (`upstream.py`); consent per era (`server.py:154-172`); link 5 min, single person and browser (`main.py:69, 425-443, 517`); saved tool lists (`server.py:206-231`); Cloudflare 12 read scopes + `offline_access` (registry) |
| quickstart.md | Ports, users alice/bob (realm), demo client port 33418, stand-in auto-consent (`mock-vendor/main.py:184`), healthz body (`lifecycle.py:108`), redis and multi commands, `PTTL vtb:sweep-lease` |
| mcp-gateway.md | FastMCP 4.0.10, Keycloak 26.7.4; poll grants every 2 s (`server.py:52, 181`); realm DCR policy; catalog reconciliation (`server.py:206-231`); 401 `WWW-Authenticate` (`test_gateway_auth.py:58`); `gateway.auth` reasons (`server.py:311-372`); config defaults (`config.py:116-132`); gateway lock has 80 pins |
| mcp-integration.md, api.md | 7-route table; problem titles and statuses; resolve body rules (`main.py:87-105`); cookie attributes (`main.py:471-473`); DELETE semantics (`main.py:697-772`); admin rules (`main.py:798-812`); health semantics (`lifecycle.py:100-114`); MCP spec links resolve |
| operations.md | Every documented default equals `config.py`; startup checks (`lifecycle.py:36-72`); renewal and health (`lifecycle.py:75-114`); runbook causes; sweep budget math; custody key encoding and migration (`custody.py:35-216`) |
| design.md, token-lifecycle.md, ADR-0001 | Assertion claims (`client_auth.py`); RFC 9207/8707/7009 behavior; scope math; waiter and lock timings (`coordination.py`, `main.py:322-411`); mass-STALE; redis key names, Lua renew, jitter, pub/sub |
| security.md | All named tests exist; leak-test coverage lists; gateway JWKS 1 h; verification counts 433/64/47 match collection |
| smoke-tests.md | Ports and containers; `/_test/token` kinds (`hub-stub/main.py:60-101`); mock behaviors; replay page text |
| CHANGELOG | New titles, `LOCK_TTL_MS` 20000, Keycloak and FastMCP versions, tool counts, audit events, realm cleanup |
