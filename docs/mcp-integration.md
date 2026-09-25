# Integrate with MCP

**Baseline: MCP 2026-07-28 · reviewed 2026-09-25.** The broker implements an internal REST contract. You supply the MCP server and trusted gateway integration described here.

## Three authorization boundaries

| Boundary | Credential and validator | Owner |
|---|---|---|
| MCP client → MCP server | Access token issued for that MCP resource; validated by the MCP server | Your MCP client, server, and authorization server |
| Gateway → broker | Internal hub JWT, revalidated by `HubValidator` | Your identity handoff and this broker |
| Gateway → vendor API | Vendor access token obtained through broker consent | Gateway, broker, vendor |

The hub JWT's `mcp_contract` claim, `mcp://tier/*` audiences, and algorithm policy are project conventions. They are not MCP protocol versions or standard MCP claim names. Do not blindly forward an MCP-server token to the broker: arrange an internal credential valid for the broker's configured issuer, audience, and contract. That identity handoff is outside this repository.

At the MCP boundary, implement protected-resource metadata, authorization-server discovery, resource indicators, intended-audience validation, and appropriate HTTP authorization challenges. The broker does not expose those MCP endpoints. [MCP authorization requirements](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)

## Calling the broker

1. Validate and authorize the MCP tool operation in your server.
2. Map that operation to an approved vendor and required vendor scopes.
3. Have the trusted gateway call [resolve](api.md#resolve-a-token) with the internal hub JWT.
4. On success, use the token only for the approved vendor API call. Keep it out of tool results, model context, browser storage, and logs.
5. Return business data to the MCP client. Strip the hub JWT from the vendor request.

The broker returns tokens to any caller satisfying its internal authentication contract. Network isolation and gateway workload authentication are therefore deployment requirements, not properties the REST response can enforce.

## Connecting a vendor during a tool call

**Proposed adapter behavior; not shipped in this repository.** A missing vendor connection is distinct from a client's authorization to call the MCP server.

For MCP 2026-07-28, an adapter can put URL-mode `elicitation/create` in an `InputRequiredResult` when the client advertises `elicitation.url`. Point its URL at the broker's `authorize_uri`; the MCP bearer token stays unchanged. The client asks permission before navigation. Use the negotiated protocol revision's SDK to build the full envelope. [URL-mode elicitation](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation)

The nested request has this shape (illustrative; the URL comes from resolve):

```json
{
  "method": "elicitation/create",
  "params": {
    "mode": "url",
    "message": "Connect your vendor account to continue this operation.",
    "url": "https://broker.example.com/v1/authorize/acme?txn=opaque-handle"
  }
}
```

Adapter decisions:

| Broker outcome | Adapter action |
|---|---|
| 200 | Call the approved vendor using the returned token |
| 404 `needs-consent` | Request a vendor connection through the supported browser flow |
| 409 `needs-reconsent-scope` | Explain additional vendor permissions and use the supplied connection URL |
| 409 `revoke-pending` | Report that disconnect is pending; do not restart consent automatically |
| 503 | Apply bounded retry/backoff; do not interpret an outage as missing consent |
| 401 `invalid-hub-token` | Repair internal authentication; do not assume the MCP client's own token expired |

After a browser flow, resolve again with the same authenticated user and required scopes. User acceptance alone does not prove the connection succeeded. A suggested adapter policy is at most one consent retry per operation before returning an actionable error. Stop on decline or cancellation. If URL elicitation is unsupported, return a clear tool error and a supported manual connection route in your application; do not ask for vendor tokens in chat or form fields.

Test the adapter separately for cancellation, unsupported capabilities, expired connection URLs, scope expansion, identity handoff, token redaction, and retry limits. Broker integration tests do not exercise an MCP client.

## Existing gateway compatibility

The internal broker API remains unchanged. The source lab's Kong plugin used custom client-facing `401 authorization_required` and 401 step-up responses for broker 404/409 consent results. That is a legacy adapter convention, not a demonstrated current MCP wire flow. The plugin is not included here. See [the legacy mapping](api.md#legacy-gateway-mapping) when preserving an existing deployment.

## Protocol revision and enterprise authorization

Use the MCP implementation's negotiated revision for capabilities and message envelopes. The 2026-07-28 release introduces per-request protocol capabilities, Multi Round-Trip Requests, and routing headers, and deprecates Dynamic Client Registration in favor of Client ID Metadata Documents. These are integration-layer responsibilities. [Release notes](https://blog.modelcontextprotocol.io/posts/2026-07-28/)

Enterprise-Managed Authorization is an optional extension involving the client, enterprise IdP, and resource authorization server. This broker does not implement ID-JAG exchange. Its `ema_status` registry field tracks migration readiness only; changing it does not disable consent or drain tokens. Evaluate whether a replacement also supplies the downstream vendor access your tools need. [Enterprise-Managed Authorization](https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization)
