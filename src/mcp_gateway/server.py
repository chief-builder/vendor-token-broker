"""The gateway's MCP server.

Per tool call: verify the caller's MCP token (FastMCP auth), exchange it at
the hub for a hub JWT, resolve the user's vendor token at the broker, and
forward the call to the vendor's MCP server in a fresh session. A missing
vendor connection becomes a URL-mode elicitation pointing at the broker's
consent link; an outage becomes a retryable error, never "connect".

Tools: `connect_github` always; the allowlisted upstream tools appear (with
the vendor's own schemas) after the first connected call, announced with
notifications/tools/list_changed. The vendor token is used for one upstream
call and never logged, cached, or returned to the client.
"""
import asyncio
import json
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any

import httpx
import mcp_types
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token, get_context
from fastmcp.tools.base import InputRequiredToolResult, Tool, ToolResult

from .clients import Broker, HandoffError, Hub, Unavailable
from .config import GatewayConfig
from .upstream import Upstream

log = logging.getLogger("mcp_gateway")

URL_ELICITATION = mcp_types.ClientCapabilities(
    elicitation=mcp_types.ElicitationCapability(url=mcp_types.UrlElicitationCapability()))
MRTR_ERA = "2026-07-28"      # from this revision, client input is a multi-round-trip result
CONSENT_KEY = "connect"
POLL_S = 2.0


def audit(event: str, **fields) -> None:
    """One JSON line per decision: ids and outcomes only, never tokens."""
    log.info(json.dumps({"event": event, "ts": round(time.time(), 3), **fields}))


def _mcp_token() -> tuple[str, str]:
    """(raw MCP access token, sub) of the verified caller."""
    token = get_access_token()
    if token is None:                   # auth middleware guarantees this in production
        raise ToolError("not authenticated")
    return token.token, str(token.claims.get("sub", ""))


class Gateway:
    def __init__(self, cfg: GatewayConfig, hub: Hub, broker: Broker, upstream: Upstream,
                 current_token: Callable[[], tuple[str, str]] = _mcp_token):
        self.cfg, self.hub, self.broker, self.upstream = cfg, hub, broker, upstream
        self.current_token = current_token
        self.catalog_loaded = False
        self._catalog_lock = asyncio.Lock()
        self.mcp: FastMCP | None = None

    # ---------------------------------------------------------- token path

    async def vendor_token(self, ctx: Context, tool: str) -> str | mcp_types.InputRequiredResult:
        """The caller's vendor token, running the consent flow if needed.
        On the 2026-07-28 revision this whole tool body re-runs after the
        client answers, so everything before the answer check is idempotent."""
        raw, sub = self.current_token()
        try:
            hub_jwt = await self.hub.exchange(raw)
            era = ctx.request_context.protocol_version
            answer = (ctx.input_responses or {}).get(CONSENT_KEY) if era >= MRTR_ERA else None
            if answer is not None:                          # MRTR: the client answered
                return await self._after_answer(hub_jwt, answer.action, sub, tool)
            resolved = await self.broker.resolve(hub_jwt)
            if resolved.token:
                return resolved.token
            if not resolved.consent_url:
                audit("gateway.call", tool=tool, sub=sub, outcome="deny",
                      reason=resolved.problem)
                raise ToolError(f"GitHub is not usable right now ({resolved.problem}); "
                                "try again later")
            return await self._ask_to_connect(ctx, era, hub_jwt, resolved.consent_url, sub, tool)
        except HandoffError as exc:
            audit("gateway.call", tool=tool, sub=sub, outcome="error", reason=str(exc))
            raise ToolError("the gateway could not establish your identity with the "
                            "token broker; this is a gateway configuration problem") from exc
        except Unavailable as exc:
            audit("gateway.call", tool=tool, sub=sub, outcome="unavailable", reason=str(exc))
            raise ToolError(f"a dependency is unavailable ({exc}); retry shortly") from exc

    async def _ask_to_connect(self, ctx: Context, era: str, hub_jwt: str, url: str,
                              sub: str, tool: str) -> str | mcp_types.InputRequiredResult:
        message = "Connect your GitHub account to continue."
        if not ctx.session.check_client_capability(URL_ELICITATION):
            audit("gateway.consent", tool=tool, sub=sub, outcome="manual")
            raise ToolError(f"Connect GitHub first: open {url} in your browser, "
                            "then retry.")
        audit("gateway.consent", tool=tool, sub=sub, outcome="elicit", era=era)
        elicitation_id = secrets.token_urlsafe(12)
        if era >= MRTR_ERA:
            return mcp_types.InputRequiredResult(
                input_requests={CONSENT_KEY: mcp_types.ElicitRequest(
                    params=mcp_types.ElicitRequestURLParams(
                        message=message, url=url, elicitation_id=elicitation_id))},
                request_state="consent")
        result = await ctx.session.elicit_url(message, url, elicitation_id,
                                              related_request_id=ctx.request_id)
        return await self._after_answer(hub_jwt, result.action, sub, tool)

    async def _after_answer(self, hub_jwt: str, action: str, sub: str, tool: str) -> str:
        """The user answered the connect prompt. Accept only means they chose
        to open the link, so wait (bounded) for the browser flow to finish."""
        if action != "accept":
            audit("gateway.consent", tool=tool, sub=sub, outcome=action)
            raise ToolError("GitHub was not connected; the request was cancelled")
        deadline = time.monotonic() + self.cfg.consent_wait_s
        while not await self.broker.connected(hub_jwt):
            if time.monotonic() >= deadline:
                audit("gateway.consent", tool=tool, sub=sub, outcome="timeout")
                raise ToolError("GitHub is not connected yet; finish connecting in the "
                                "browser, then retry")
            await asyncio.sleep(POLL_S)
        resolved = await self.broker.resolve(hub_jwt)
        if not resolved.token:
            raise ToolError(f"GitHub connection is not usable ({resolved.problem}); retry")
        audit("gateway.consent", tool=tool, sub=sub, outcome="connected")
        return resolved.token

    # ------------------------------------------------------------- catalog

    async def load_catalog(self, ctx: Context, vendor_token: str) -> int:
        """Register the allowlisted upstream tools with the vendor's schemas,
        once per process. Returns how many are available."""
        async with self._catalog_lock:
            if not self.catalog_loaded:
                tools = await self.upstream.list_tools(vendor_token)
                wanted = set(self.cfg.upstream_tools)
                found = [t for t in tools if t.name in wanted]
                for t in found:
                    self.mcp.add_tool(UpstreamTool(
                        gateway=self, name=t.name, description=t.description or "",
                        parameters=t.input_schema, annotations=t.annotations))
                missing = sorted(wanted - {t.name for t in found})
                audit("gateway.catalog", loaded=[t.name for t in found], missing=missing)
                self.catalog_loaded = True
                await ctx.send_notification(mcp_types.ToolListChangedNotification())
        return len([t for t in await self.mcp.list_tools() if isinstance(t, UpstreamTool)])

    async def forward(self, ctx: Context, name: str, arguments: dict[str, Any]) -> ToolResult:
        token = await self.vendor_token(ctx, name)
        if isinstance(token, mcp_types.InputRequiredResult):
            return InputRequiredToolResult(token)
        _, sub = self.current_token()
        try:
            result = await self.upstream.call_tool(token, name, arguments)
        except Exception as exc:     # transport or protocol failure upstream
            rejected = "401" in str(exc)
            audit("gateway.call", tool=name, sub=sub, outcome="upstream-error",
                  reason="rejected" if rejected else type(exc).__name__)
            if rejected:
                raise ToolError("GitHub rejected the connection; reconnect GitHub "
                                "and retry") from exc
            raise ToolError("GitHub's MCP server is unavailable; retry shortly") from exc
        audit("gateway.call", tool=name, sub=sub, outcome="ok", is_error=result.is_error)
        return ToolResult.from_mcp_result(result)


class UpstreamTool(Tool):
    """An allowlisted vendor tool: the vendor's schema, forwarded per call.
    Arguments are validated by the vendor, not here."""
    gateway: Any = None

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        return await self.gateway.forward(get_context(), self.name, arguments)


def build_server(gateway: Gateway, auth=None) -> FastMCP:
    mcp = FastMCP("vtb-mcp-gateway", auth=auth)
    gateway.mcp = mcp

    @mcp.tool
    async def connect_github(ctx: Context) -> str | mcp_types.InputRequiredResult:
        """Connect your GitHub account (a browser opens if it isn't connected
        yet) and make the GitHub tools available. Call this first."""
        token = await gateway.vendor_token(ctx, "connect_github")
        if isinstance(token, mcp_types.InputRequiredResult):
            return token
        count = await gateway.load_catalog(ctx, token)
        return f"GitHub is connected; {count} GitHub tools are available."

    return mcp


def build_auth(cfg: GatewayConfig, verifier: JWTVerifier | None = None) -> RemoteAuthProvider:
    """The gateway as an OAuth protected resource: protected-resource
    metadata naming the hub, and MCP tokens checked for signature (pinned
    algorithm), issuer, audience = this resource, and the gateway scope.
    Clients are told to request the scope because the hub may not support
    RFC 8707 resource indicators (Keycloak doesn't)."""
    verifier = verifier or JWTVerifier(
        jwks_uri=cfg.hub_jwks_uri, issuer=cfg.hub_issuer, audience=cfg.resource_url,
        algorithm=cfg.hub_algorithm, required_scopes=[cfg.gateway_scope])
    return RemoteAuthProvider(token_verifier=verifier, authorization_servers=[cfg.hub_issuer],
                              base_url=cfg.public_url, scopes_supported=[cfg.gateway_scope],
                              resource_name="vtb mcp-gateway")


def create_app():
    """uvicorn --factory mcp_gateway.server:create_app"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = GatewayConfig.from_env()
    http = httpx.AsyncClient(timeout=cfg.http_timeout_s)
    gateway = Gateway(cfg, Hub(cfg, http), Broker(cfg, http), Upstream(cfg))
    return build_server(gateway, auth=build_auth(cfg)).http_app(path="/mcp")
