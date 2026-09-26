# Integrate with MCP

**Baseline: MCP 2026-07-28 · reviewed 2026-09-26.**

Use this page to connect **your own** MCP server or gateway to the broker.

- **Just want GitHub?** You do not need this page. The shipped [MCP gateway](mcp-gateway.md) already connects GitHub's MCP server to the broker and handles all the MCP work described here.
- The broker itself does not speak MCP. It offers an internal REST API ([API reference](api.md)). Your gateway sits between MCP clients and that API.

## Three authorization boundaries

Three separate credentials are in play. Keep them apart.

| Boundary | Credential and validator | Owner |
|---|---|---|
| MCP client → MCP server | Access token issued for that MCP resource; checked by the MCP server | Your MCP client and authorization server; the shipped gateway checks it |
| Gateway → broker | Internal hub JWT, checked again by `HubValidator` | Your identity handoff and this broker |
| Gateway → vendor API | Vendor access token obtained through broker consent | Gateway, broker, vendor |

**The hub** is your company's sign-in service (identity provider). The **hub JWT** is the internal token it issues, which the broker accepts.

**Do not forward the MCP client's token to the broker.** Get an internal credential that matches the broker's configured issuer, audience, and contract instead. The shipped gateway does this with a [token exchange at the hub](mcp-gateway.md#identity-handoff-what-the-hub-must-do) (RFC 8693).

These parts of the hub JWT are this project's own conventions. They are not MCP protocol versions or standard MCP claim names:

- the `mcp_contract` claim
- the `mcp://tier/*` audiences
- the algorithm policy

**Your MCP server must handle MCP authorization itself.** The broker does not provide these MCP endpoints. The shipped gateway does. You need:

- protected-resource metadata
- authorization-server discovery
- resource indicators
- checking the token's intended audience
- the right HTTP authorization challenges

See [MCP authorization requirements](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization).

## Calling the broker

For each tool call that needs a vendor:

1. Check and authorize the MCP tool operation in your server.
2. Map the operation to an approved vendor and the vendor scopes it needs.
3. Have the trusted gateway call [resolve](api.md#resolve-a-token) with the internal hub JWT.
4. On success, use the token only for the approved vendor API call. Keep it out of tool results, model context, browser storage, and logs.
5. Return business data to the MCP client. Remove the hub JWT from the vendor request.

The broker gives a token to any caller that passes its internal authentication. So you must isolate the network and authenticate the gateway workload when you deploy. The REST response cannot enforce this for you.

## Connecting a vendor during a tool call

**The shipped gateway already does this. See [its consent behavior](mcp-gateway.md#connecting-github-during-a-tool-call).** The guidance below applies to any adapter.

A missing vendor connection is a different problem from the client's permission to call the MCP server. Handle it separately.

**How to ask the user to connect (MCP 2026-07-28):**

- If the client advertises `elicitation.url`, put a URL-mode `elicitation/create` inside an `InputRequiredResult`.
- Set its URL to the broker's `authorize_uri`.
- The MCP bearer token stays the same.
- The client asks the user for permission before it opens the URL.
- Use the SDK for the negotiated protocol revision to build the full envelope.

See [URL-mode elicitation](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation).

The nested request looks like this. This is an example; the URL comes from resolve. Clients on 2025-11-25 get the same request directly as `elicitation/create` during the call.

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

**What the adapter should do with each broker result:**

| Broker outcome | Adapter action |
|---|---|
| 200 | Call the approved vendor with the returned token |
| 404 `needs-consent` | Ask for a vendor connection through the supported browser flow |
| 409 `needs-reconsent-scope` | Explain the extra vendor permissions and use the supplied connection URL |
| 409 `revoke-pending` | Say that a disconnect is pending. Do not restart consent automatically |
| 503 | Retry a limited number of times with backoff. Do not treat an outage as missing consent |
| 401 `invalid-hub-token` | Fix internal authentication. Do not assume the MCP client's own token expired |

**After the browser flow:**

- Confirm the connection before you resolve again, with the same user and required scopes.
- The user accepting the prompt does not prove the connection worked.
- Each failed resolve creates a new connection link. So while you wait, poll the [grant list](api.md#list-and-disconnect), not resolve.
- A suggested policy: at most one consent retry per operation, then return an error the user can act on.
- Stop if the user declines or cancels.
- If the client does not support URL elicitation, return a clear tool error. Point to a manual connection route that your application supports.
- Never ask for vendor tokens in chat or form fields.

**Test these cases in your adapter:** cancellation, unsupported capabilities, expired connection URLs, scope expansion, identity handoff, token redaction, and retry limits. The gateway's end-to-end tests (`tests/integration/test_mcp_gateway.py`) cover these with a real MCP client.

**List all tools from the start.** Do not count on `notifications/tools/list_changed` to reveal tools after a connection. From 2026-07-28, servers may only send it on `subscriptions/listen`. The [gateway's pinned tool list](mcp-gateway.md#tools) shows this approach.

## Existing gateway compatibility

The internal broker API has not changed.

- The source lab's Kong plugin turned broker 404/409 consent results into custom client-facing `401 authorization_required` and 401 step-up responses.
- That is an old adapter convention. It has not been shown to work as a current MCP wire flow.
- The plugin is not in this repository.

If you are keeping an existing deployment, see [the legacy mapping](api.md#legacy-gateway-mapping).

## Protocol revision and enterprise authorization

**Use the revision your MCP implementation negotiated** for capabilities and message envelopes. The 2026-07-28 release:

- adds per-request protocol capabilities, Multi Round-Trip Requests, and routing headers
- deprecates Dynamic Client Registration in favor of Client ID Metadata Documents

Your integration layer handles these, not the broker. See the [release notes](https://blog.modelcontextprotocol.io/posts/2026-07-28/).

**Enterprise-Managed Authorization** is an optional extension. It involves the client, the enterprise identity provider, and the resource's authorization server.

- This broker does not implement ID-JAG exchange.
- The registry field `ema_status` only tracks how ready a vendor is to migrate. Changing it does not turn off consent or remove tokens.
- If you consider it as a replacement, check that it also gives your tools the vendor access they need.

See [Enterprise-Managed Authorization](https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization).
