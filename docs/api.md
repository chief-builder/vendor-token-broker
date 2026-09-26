# Broker API reference

Use this page if you are writing your own gateway that calls the broker. It is the main reference for the broker's internal REST API.

- This is not the MCP protocol. MCP clients never talk to the broker directly.
- If you only want GitHub's MCP server, use the shipped [MCP gateway](mcp-gateway.md). It already calls this API for you.
- [Integrate with MCP](mcp-integration.md) explains how an MCP server or gateway sits in front of the broker.

## Authentication and routes

The broker has exactly seven routes. Most of them need a hub JWT.

- **The hub** is your company's sign-in service (identity provider). A **hub JWT** is the signed token it issues for a user.
- Send `Authorization: Bearer <hub-jwt>` on resolve, list, delete, and admin requests.
- The broker trusts the JWT subject (`sub`) as the user's identity.
- The consent routes do not use a bearer token. They use short-lived transaction state and a cookie that ties the flow to one browser.
- `/healthz` needs no authentication.

| Method | Route | Purpose |
|---|---|---|
| POST | `/v1/tokens/resolve` | Get a vendor token for the caller |
| GET | `/v1/authorize/{vendor}?txn=…` | Start browser consent |
| GET | `/v1/callback/{vendor}` | Finish one consent step; `_hub` is the callback for the hub sign-in step |
| GET | `/v1/grants` | List the caller's connections, without any tokens |
| DELETE | `/v1/grants/{vendor}/{sub}` | Disconnect the caller from a vendor |
| GET | `/v1/admin/vendors/{vendor}` | Read one registry entry; the JWT's `groups` array must contain `ADMIN_GROUP` |
| GET | `/healthz` | Report whether this replica's custody token is healthy |

The broker has no endpoint that issues tokens. It also has no JWKS, OpenAPI, Swagger, or ReDoc endpoint. `tests/unit/test_routes.py` checks this.

## Resolve a token

Call resolve to get a live vendor token for the signed-in user. The broker may refresh the token during the call.

```http
POST /v1/tokens/resolve
Authorization: Bearer <internal-hub-jwt>
Content-Type: application/json

{"vendor":"mockhub","min_ttl_s":30,"required_scopes":["issues:read"]}
```

| Field | Default | Meaning |
|---|---|---|
| `vendor` | Required | ID of an enabled vendor in the registry |
| `sub` | JWT subject | Optional. If you send a non-empty value that differs from the JWT subject, the broker rejects the request |
| `min_ttl_s` | 120 | Non-negative integer. How long the token should still be valid, in seconds. Capped at `REFRESH_BUFFER_S` |
| `required_scopes` | `[]` | Array of scope strings. The vendor's scope policy decides how these are used |

On success the broker returns HTTP 200 with:

- `access_token` — the vendor token. Keep it inside the trusted gateway.
- `expires_at` — expiry time in Unix seconds. It may have a fractional part.
- `granted_scopes` — the scopes the broker has recorded for this connection.

**Lifetime is a target, not a guarantee.**

- A vendor can issue a new token that lives less than `min_ttl_s`. The broker still returns it.
- Check `expires_at` before you use the token.
- If you ask for more than the refresh buffer, the broker caps the request. The audit event then includes `min_ttl_clamped_from`. On some waiter paths it also includes `short_ttl`.

### Scope policy

Each vendor in the registry has a scope ceiling: the most scopes the broker will ever ask for.

When the ceiling is **not empty**:

- It caps both requested and recorded scopes.
- A request for scopes outside the ceiling fails with 403.
- If a caller sends no `required_scopes`, first-time consent asks for the whole ceiling. This keeps older callers working.
- When the broker asks for more scopes, it asks for the scopes already held plus the new required ones, within the ceiling.

When the ceiling is **empty**, the vendor manages permissions:

- The broker does not request or enforce `required_scopes` for that vendor.
- Filtering the stored `granted_scopes` list does **not** reduce what the token can actually do.

In both cases:

- If a vendor widens scopes unexpectedly, the broker writes a `scope_widened` audit event.
- Your gateway or server must still check what each operation is allowed to do.

## Errors and caller actions

Errors come back as `application/problem+json`. Branch on the HTTP status and the `title`.

- Each error has `type`, `title`, and `detail`, plus any extra fields in the table below.
- The `title` is stable. Do not parse `detail` (it is for people) or the `type` prefix (it is configurable).
- Request validation errors from the web framework may use FastAPI's 422 response shape instead.

| Status / title | Additional fields | Action |
|---|---|---|
| 400 `invalid-request` | — | Fix the malformed resolve input |
| 400 `sub-mismatch` | — | Use the signed-in user's subject |
| 400 `invalid-transaction` | — | Get a fresh connection URL |
| 401 `invalid-hub-token` | — | Fix the internal token's issuer, signature, expiry, audience, or contract |
| 403 `scope-exceeds-ceiling` | `required`, `ceiling` | Review the vendor's policy. Do not loop through consent |
| 403 `forbidden` | — | Check the self-service subject or the admin group |
| 404 `unknown-vendor` | — | Check the registry and whether the vendor is enabled |
| 404 `needs-consent` | `authorize_uri` | No usable connection. Connect or reconnect |
| 404 `no-grant` | — | Nothing to delete. The connection is already gone |
| 409 `needs-reconsent-scope` | `authorize_uri`, `missing_scopes` | Ask the user for more vendor permissions |
| 409 `revoke-pending` | — | Resolve cannot use a connection that is being revoked |
| 502 `revoke-pending` | — | Delete could not revoke at the vendor. The sweeper will retry |
| 503 `vendor-unavailable` | — | Retry with backoff. Check whether the vendor is up or refreshes are contending |
| 503 `vault-unavailable` | — | Restore custody (token storage). Do not ask users to reconnect |
| 503 `coordination-unavailable` | — | Restore Redis for the operations that need coordination |
| 503 `hub-unavailable` | — | Restore access to the hub's JWKS, or the hub itself |

The consent callbacks are browser pages, so they mostly return short HTML pages:

- 400 — bad state, issuer, or browser
- 403 — wrong user
- 502 — the token exchange failed
- 503 — a dependency is down

One exception: if the hub sign-in callback cannot start the vendor step, it returns problem JSON 503 (`vendor-unavailable`, `vault-unavailable`, or `coordination-unavailable`). [Consent internals](design.md#43-get-v1callbackvendorcodestate) explain how the broker handles state.

## Browser consent

Consent is how a user connects their vendor account. It happens in the user's browser in two steps.

1. The user opens the `authorize_uri` from resolve. They must open it within five minutes.
2. The broker checks who the user is through the hub.
3. The broker then starts vendor consent for that same user.

Rules:

- The same browser must finish both steps. Keep cookies across redirects.
- Consent state lasts ten minutes by default.
- The binding cookie is HttpOnly and SameSite=Lax. It gets the Secure flag only when `BROKER_PUBLIC_URL` uses HTTPS.
- Use the exact registered callback URLs.
- If the transaction expired, was cancelled, or was already used, start again with a new resolve or connection attempt.

## List and disconnect

**List.** `GET /v1/grants` returns `{"grants":[…]}`.

- Each entry has `vendor`, `state`, `granted_scopes`, `vendor_user_id`, and `created_at`.
- The internal `REFRESHING` state shows as `ACTIVE`.
- Tokens are never included.

**Disconnect.** `DELETE /v1/grants/{vendor}/{sub}` removes a connection.

- The `sub` in the path must match the JWT subject. URL-encode it when you build the path.
- Success returns `{"revoked":true}`.
- If the vendor does not support revocation, success also includes `"vendor_revocation":"unsupported"`. The broker deletes its own copy, but the vendor may still honour the permissions. Disconnect at the vendor too if you need to.

## Health

`GET /healthz` normally returns `{"ok":true,"custody":"ok"}`.

- If custody is down, it can still return HTTP 200 with `"custody":"unreachable"`. This keeps replicas serving valid cached entries.
- If the custody credentials are rejected or nearly expired, it returns 503.
- The broker caches health responses for ten seconds.

See [the runbook](operations.md#runbook) for what to do next.

## Legacy gateway mapping

This table shows how the source lab's gateway translated broker results. `tests/integration/test_wire_compat.py` covers the broker side. The gateway plugin itself is not in this repository.

| Broker result | Legacy gateway behavior |
|---|---|
| 200 | Replace upstream Authorization with the vendor access token |
| 404 with `authorize_uri` | Custom 401 `authorization_required` challenge |
| 409 with `authorize_uri` | Custom 401 step-up challenge |
| Plain 409 | 403 carrying the problem title |
| 5xx | Retriable 503 `vendor_unavailable` |

For a new MCP adapter, follow [the integration guide](mcp-integration.md#connecting-a-vendor-during-a-tool-call) instead. Change the client-facing protocol if you need to, but keep this internal API as it is.
