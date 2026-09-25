# Broker API reference

This is the authoritative caller reference for the internal REST API. It is not the MCP wire protocol. [Integrate with MCP](mcp-integration.md) explains the adapter boundary.

## Authentication and routes

Send `Authorization: Bearer <hub-jwt>` on resolve, list, delete, and admin requests. The JWT subject is authoritative. Consent routes use short-lived transaction state and a browser-binding cookie. Health is unauthenticated.

| Method | Route | Purpose |
|---|---|---|
| POST | `/v1/tokens/resolve` | Retrieve a vendor token for the caller |
| GET | `/v1/authorize/{vendor}?txn=…` | Start browser consent |
| GET | `/v1/callback/{vendor}` | Complete a consent leg; `_hub` is the hub-login callback |
| GET | `/v1/grants` | List the caller's connections without token material |
| DELETE | `/v1/grants/{vendor}/{sub}` | Disconnect the caller's vendor grant |
| GET | `/v1/admin/vendors/{vendor}` | Read a registry entry; requires `ADMIN_GROUP` in the JWT's `groups` array |
| GET | `/healthz` | Report this replica's custody-token health |

The service has no token-issuance, JWKS, OpenAPI, Swagger, or ReDoc endpoint. See `tests/unit/test_routes.py`.

## Resolve a token

```http
POST /v1/tokens/resolve
Authorization: Bearer <internal-hub-jwt>
Content-Type: application/json

{"vendor":"mockhub","min_ttl_s":30,"required_scopes":["issues:read"]}
```

| Field | Default | Meaning |
|---|---|---|
| `vendor` | Required | Enabled registry vendor ID |
| `sub` | JWT subject | Optional advisory subject; a non-empty mismatch is rejected |
| `min_ttl_s` | 120 | Non-negative integer; target remaining lifetime, clamped to `REFRESH_BUFFER_S` |
| `required_scopes` | `[]` | Array of scope strings; governed by the vendor's scope policy |

Success returns HTTP 200 with `access_token`, Unix-seconds `expires_at` (may be fractional), and `granted_scopes`. Handle the token only inside the trusted gateway. Refresh can occur inline.

**Lifetime is a target, not an unconditional guarantee.** A newly issued vendor token may live less than the requested minimum and can still be returned. Inspect `expires_at` before use. Requests above the refresh buffer are clamped; audit fields include `min_ttl_clamped_from` and, on relevant waiter paths, `short_ttl`.

### Scope policy

A non-empty registry ceiling caps requested and recorded scopes. Requests beyond it fail with 403. With no required scopes, initial consent requests the full ceiling for legacy callers. Scope expansion unions held and required scopes within the ceiling.

An empty ceiling means permissions are vendor-managed: the broker does not request or enforce `required_scopes` for that vendor. Filtering the stored `granted_scopes` list does **not** reduce actual permissions on the token. Unexpected vendor scope widening is audited as `scope_widened`; enforce operation-level authorization in the gateway/server too.

## Errors and caller actions

Broker problem responses use `application/problem+json` with `type`, `title`, and `detail`, plus fields listed below. Branch on HTTP status and the stable `title`; do not parse human-readable `detail` or the configurable `type` prefix. Framework request-validation errors may instead use FastAPI's 422 response shape.

| Status / title | Additional fields | Action |
|---|---|---|
| 400 `invalid-request` | — | Correct malformed resolve input |
| 400 `sub-mismatch` | — | Use the authenticated subject |
| 400 `invalid-transaction` | — | Obtain a fresh connection URL |
| 401 `invalid-hub-token` | — | Correct internal token issuer, signature, expiry, audience, or contract |
| 403 `scope-exceeds-ceiling` | `required`, `ceiling` | Review vendor policy; do not loop consent |
| 403 `forbidden` | — | Check self-service subject or admin group |
| 404 `unknown-vendor` | — | Check registry and activation setting |
| 404 `needs-consent` | `authorize_uri` | No usable grant: connect or reconnect |
| 404 `no-grant` | — | Delete target is already absent |
| 409 `needs-reconsent-scope` | `authorize_uri`, `missing_scopes` | Obtain additional vendor consent |
| 409 `revoke-pending` | — | Resolve cannot use a connection being revoked |
| 502 `revoke-pending` | — | Delete could not revoke upstream; sweeper will retry |
| 503 `vendor-unavailable` | — | Retry with backoff; inspect vendor availability / refresh contention |
| 503 `vault-unavailable` | — | Restore custody; do not ask users to reconnect |
| 503 `coordination-unavailable` | — | Restore Redis for operations requiring coordination |
| 503 `hub-unavailable` | — | Restore hub JWKS access / hub availability |

Consent callbacks are browser endpoints. They mostly return short HTML pages (400 bad state, issuer, or browser; 403 wrong user; 502 token exchange failed; 503 dependency down). When the hub sign-in callback cannot start the vendor leg, it returns problem JSON 503 instead (`vendor-unavailable`, `vault-unavailable`, or `coordination-unavailable`). [Consent internals](design.md#43-get-v1callbackvendorcodestate) describe state handling.

## Browser consent

Open the supplied `authorize_uri` within five minutes. Keep cookies across redirects. The broker first verifies the browser user's identity through the hub, then begins vendor consent for the same subject. Consent state defaults to ten minutes. The same browser must complete both legs.

The binding cookie is HttpOnly and SameSite=Lax; its Secure flag requires an HTTPS `BROKER_PUBLIC_URL`. Use the exact registered callback URLs. An expired, cancelled, or consumed transaction requires a new resolve/connection attempt.

## List and disconnect

`GET /v1/grants` returns `{"grants":[…]}`. Each entry contains `vendor`, `state`, `granted_scopes`, `vendor_user_id`, and `created_at`. Internal `REFRESHING` is reported as `ACTIVE`; tokens are omitted.

`DELETE /v1/grants/{vendor}/{sub}` requires the path subject to match the JWT subject. URL-encode the subject when constructing the path. Success is `{"revoked":true}`. If upstream revocation is unsupported, success additionally includes `"vendor_revocation":"unsupported"`: the local connection is deleted, but vendor permissions can survive. Disconnect at the vendor if required.

## Health

Normal response: `{"ok":true,"custody":"ok"}`. Custody outage can return HTTP 200 with `"custody":"unreachable"` to preserve replicas serving valid cached entries. Rejected or nearly expired custody credentials return 503. Health responses are cached for ten seconds. See [the runbook](operations.md#runbook).

## Legacy gateway mapping

The source lab's gateway expected the following mapping. The broker side is covered by `tests/integration/test_wire_compat.py`; the gateway plugin itself is not in this repository.

| Broker result | Legacy gateway behavior |
|---|---|
| 200 | Replace upstream Authorization with the vendor access token |
| 404 with `authorize_uri` | Custom 401 `authorization_required` challenge |
| 409 with `authorize_uri` | Custom 401 step-up challenge |
| Plain 409 | 403 carrying the problem title |
| 5xx | Retriable 503 `vendor_unavailable` |

For a current MCP adapter, use [the integration guide](mcp-integration.md#connecting-a-vendor-during-a-tool-call). Preserve this internal API while adapting the client-facing protocol.
