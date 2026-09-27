"""The gateway's MCP server, in front of one or more vendor MCP servers
("upstreams", configured in upstreams.json).

Per tool call: verify the caller's MCP token (FastMCP auth), exchange it at
the hub for a hub JWT, resolve the user's token for that upstream's vendor
at the broker, and forward the call to the vendor's MCP server in a fresh
session. A missing vendor connection becomes a URL-mode elicitation
pointing at the broker's consent link; an outage becomes a retryable error,
never "connect".

Tools: for each upstream, `connect_<name>` plus its allowlisted tools
exposed as `<name>_<tool>`, listed from startup with the schemas in a
checked-in snapshot so no client ever needs a list-changed notification
(2026-07-28 clients only take those on subscriptions/listen). The first
connected call per upstream re-reads the live schemas, which stay
authoritative, and logs any drift. Vendor tokens are used for one upstream
call and never logged, cached, or returned.
"""
import asyncio
import json
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import mcp_types
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token, get_context
from fastmcp.tools.base import InputRequiredToolResult, Tool, ToolResult
from starlette.types import ASGIApp, Receive, Scope, Send

from .clients import Broker, HandoffError, Hub, Unavailable
from .config import GatewayConfig, UpstreamSpec
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


class Route:
    """One upstream as the gateway serves it: its spec, its MCP client, and
    whether its live catalog has been reconciled in this process."""

    def __init__(self, spec: UpstreamSpec, client):
        self.spec, self.client = spec, client
        self.catalog_loaded = False
        self.lock = asyncio.Lock()

    def exposed(self, tool: str) -> str:
        return f"{self.spec.name}_{tool}"


class Gateway:
    def __init__(self, cfg: GatewayConfig, hub: Hub, broker: Broker, routes: list[Route],
                 current_token: Callable[[], tuple[str, str]] = _mcp_token):
        self.cfg, self.hub, self.broker = cfg, hub, broker
        self.routes = {r.spec.name: r for r in routes}
        self.current_token = current_token
        self.mcp: FastMCP | None = None

    # ---------------------------------------------------------- token path

    async def vendor_token(self, ctx: Context, route: Route,
                           tool: str) -> str | mcp_types.InputRequiredResult:
        """The caller's token for this route's vendor, running the consent
        flow if needed. On the 2026-07-28 revision this whole tool body
        re-runs after the client answers, so everything before the answer
        check is idempotent."""
        raw, sub = self.current_token()
        who = {"upstream": route.spec.name, "tool": tool, "sub": sub}
        name = route.spec.display_name
        try:
            hub_jwt = await self.hub.exchange(raw)
            era = ctx.request_context.protocol_version
            answer = (ctx.input_responses or {}).get(CONSENT_KEY) if era >= MRTR_ERA else None
            if answer is not None:                          # MRTR: the client answered
                return await self._after_answer(hub_jwt, answer.action, route, who)
            resolved = await self.broker.resolve(hub_jwt, route.spec.vendor)
            if resolved.token:
                return resolved.token
            if not resolved.consent_url:
                audit("gateway.call", **who, outcome="deny", reason=resolved.problem)
                raise ToolError(f"{name} is not usable right now ({resolved.problem}); "
                                "try again later")
            return await self._ask_to_connect(ctx, era, hub_jwt, resolved.consent_url,
                                              route, who)
        except HandoffError as exc:
            audit("gateway.call", **who, outcome="error", reason=str(exc))
            raise ToolError("the gateway could not establish your identity with the "
                            "token broker; this is a gateway configuration problem") from exc
        except Unavailable as exc:
            audit("gateway.call", **who, outcome="unavailable", reason=str(exc))
            raise ToolError(f"a dependency is unavailable ({exc}); retry shortly") from exc

    async def _ask_to_connect(self, ctx: Context, era: str, hub_jwt: str, url: str,
                              route: Route, who: dict) -> str | mcp_types.InputRequiredResult:
        name = route.spec.display_name
        message = f"Connect your {name} account to continue."
        if not ctx.session.check_client_capability(URL_ELICITATION):
            audit("gateway.consent", **who, outcome="manual")
            raise ToolError(f"Connect {name} first: open {url} in your browser, "
                            "then retry.")
        audit("gateway.consent", **who, outcome="elicit", era=era)
        elicitation_id = secrets.token_urlsafe(12)
        if era >= MRTR_ERA:
            return mcp_types.InputRequiredResult(
                input_requests={CONSENT_KEY: mcp_types.ElicitRequest(
                    params=mcp_types.ElicitRequestURLParams(
                        message=message, url=url, elicitation_id=elicitation_id))},
                request_state="consent")
        result = await ctx.session.elicit_url(message, url, elicitation_id,
                                              related_request_id=ctx.request_id)
        return await self._after_answer(hub_jwt, result.action, route, who)

    async def _after_answer(self, hub_jwt: str, action: str, route: Route, who: dict) -> str:
        """The user answered the connect prompt. Accept only means they chose
        to open the link, so wait (bounded) for the browser flow to finish."""
        name = route.spec.display_name
        if action != "accept":
            audit("gateway.consent", **who, outcome=action)
            raise ToolError(f"{name} was not connected; the request was cancelled")
        deadline = time.monotonic() + self.cfg.consent_wait_s
        while not await self.broker.connected(hub_jwt, route.spec.vendor):
            if time.monotonic() >= deadline:
                audit("gateway.consent", **who, outcome="timeout")
                raise ToolError(f"{name} is not connected yet; finish connecting in the "
                                "browser, then retry")
            await asyncio.sleep(POLL_S)
        resolved = await self.broker.resolve(hub_jwt, route.spec.vendor)
        if not resolved.token:
            raise ToolError(f"{name} connection is not usable ({resolved.problem}); retry")
        audit("gateway.consent", **who, outcome="connected")
        return resolved.token

    # ------------------------------------------------------------- catalog

    def register(self, route: Route, tool: mcp_types.Tool) -> None:
        self.mcp.add_tool(UpstreamTool(
            gateway=self, route_name=route.spec.name, upstream_tool=tool.name,
            name=route.exposed(tool.name), description=tool.description or "",
            parameters=tool.input_schema, annotations=tool.annotations))

    async def _registered(self, route: Route) -> dict[str, Tool]:
        return {t.upstream_tool: t for t in await self.mcp.list_tools()
                if isinstance(t, UpstreamTool) and t.route_name == route.spec.name}

    async def load_catalog(self, ctx: Context, route: Route, vendor_token: str) -> int:
        """Reconcile this route's listed tools with the vendor's live schemas,
        once per process: re-register tools whose schema drifted from the
        snapshot, add allowlisted tools the snapshot lacked, and report ones
        the vendor no longer lists (they stay listed; calling them returns the
        vendor's error). Returns how many of the route's tools are listed."""
        async with route.lock:
            if not route.catalog_loaded:
                allowed = set(route.spec.tools)
                live = {t.name: t for t in await route.client.list_tools(vendor_token)
                        if t.name in allowed}
                listed = await self._registered(route)
                added = sorted(set(live) - set(listed))
                changed = sorted(n for n in set(live) & set(listed)
                                 if (live[n].description or "", live[n].input_schema)
                                 != (listed[n].description, listed[n].parameters))
                for name in added + changed:
                    self.register(route, live[name])
                missing = sorted(allowed - set(live))
                audit("gateway.catalog", upstream=route.spec.name, listed=sorted(live),
                      added=added, changed=changed, missing=missing)
                route.catalog_loaded = True
                if added:        # only older clients act on this; see module docstring
                    await ctx.send_notification(mcp_types.ToolListChangedNotification())
        return len(await self._registered(route))

    async def forward(self, ctx: Context, route: Route, tool: str,
                      arguments: dict[str, Any]) -> ToolResult:
        exposed = route.exposed(tool)
        token = await self.vendor_token(ctx, route, exposed)
        if isinstance(token, mcp_types.InputRequiredResult):
            return InputRequiredToolResult(token)
        _, sub = self.current_token()
        who = {"upstream": route.spec.name, "tool": exposed, "sub": sub}
        name = route.spec.display_name
        try:
            result = await route.client.call_tool(token, tool, arguments)
        except Exception as exc:     # transport or protocol failure upstream
            rejected = "401" in str(exc)
            detail = str(exc).replace(token, "<token>")[:200]   # never log the token
            audit("gateway.call", **who, outcome="upstream-error",
                  reason="rejected" if rejected else type(exc).__name__, detail=detail)
            if rejected:
                raise ToolError(f"{name} rejected the connection; reconnect {name} "
                                "and retry") from exc
            raise ToolError(f"{name}'s MCP server is unavailable; retry shortly") from exc
        audit("gateway.call", **who, outcome="ok", is_error=result.is_error)
        return ToolResult.from_mcp_result(result)


class UpstreamTool(Tool):
    """An allowlisted vendor tool, exposed as <upstream>_<tool> with the
    vendor's schema and forwarded per call under its upstream name.
    Arguments are validated by the vendor, not here."""
    gateway: Any = None
    route_name: str = ""
    upstream_tool: str = ""

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        route = self.gateway.routes[self.route_name]
        return await self.gateway.forward(get_context(), route, self.upstream_tool, arguments)


def _connect_tool(gateway: Gateway, route: Route):
    async def connect(ctx: Context) -> str | mcp_types.InputRequiredResult:
        token = await gateway.vendor_token(ctx, route, f"connect_{route.spec.name}")
        if isinstance(token, mcp_types.InputRequiredResult):
            return token
        count = await gateway.load_catalog(ctx, route, token)
        name = route.spec.display_name
        return f"{name} is connected; {count} {name} tools are available."
    return connect


def build_server(gateway: Gateway, auth=None) -> FastMCP:
    mcp = FastMCP("vtb-mcp-gateway", auth=auth, on_duplicate="replace")
    gateway.mcp = mcp
    for route in gateway.routes.values():
        name = route.spec.display_name
        mcp.tool(_connect_tool(gateway, route), name=f"connect_{route.spec.name}",
                 description=f"Connect your {name} account (a browser opens if it isn't "
                             f"connected yet). The {name} tools start with "
                             f"{route.spec.name}_.")
        for tool in route.spec.snapshot:
            if tool.name in route.spec.tools:
                gateway.register(route, tool)
    return mcp


def _rejection_reason(verifier: JWTVerifier, token: str) -> dict:
    """Why an MCP token was refused, from its unverified header and claims.
    Ids and categories only; the token itself is never logged."""
    try:
        header = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return {"reason": "malformed"}
    ids = {"sub": claims.get("sub"), "client": claims.get("azp")}
    audiences = claims.get("aud", [])
    audiences = [audiences] if isinstance(audiences, str) else audiences
    now = time.time()
    if header.get("alg") != verifier.algorithm:
        return {**ids, "reason": "algorithm", "alg": header.get("alg")}
    if claims.get("iss") != verifier.issuer:
        return {**ids, "reason": "issuer"}
    if verifier.audience not in audiences:
        return {**ids, "reason": "audience"}
    if claims.get("exp", 0) <= now:
        return {**ids, "reason": "expired", "expired_s_ago": round(now - claims.get("exp", 0))}
    if not set(verifier.required_scopes or ()) <= set(claims.get("scope", "").split()):
        return {**ids, "reason": "scope"}
    return {**ids, "reason": "signature"}


class AuditingJWTVerifier(JWTVerifier):
    """JWTVerifier that records why each refused token was refused."""

    async def verify_token(self, token: str):
        accepted = await super().verify_token(token)
        if accepted is None:
            audit("gateway.auth", outcome="deny", **_rejection_reason(self, token))
        return accepted


def build_auth(cfg: GatewayConfig, verifier: JWTVerifier | None = None) -> RemoteAuthProvider:
    """The gateway as an OAuth protected resource: protected-resource
    metadata naming the hub, and MCP tokens checked for signature (pinned
    algorithm), issuer, audience = this resource, and the gateway scope.
    Clients are told to request the scope because the hub may not support
    RFC 8707 resource indicators (Keycloak doesn't)."""
    verifier = verifier or AuditingJWTVerifier(
        jwks_uri=cfg.hub_jwks_uri, issuer=cfg.hub_issuer, audience=cfg.resource_url,
        algorithm=cfg.hub_algorithm, required_scopes=[cfg.gateway_scope])
    return RemoteAuthProvider(token_verifier=verifier, authorization_servers=[cfg.hub_issuer],
                              base_url=cfg.public_url, scopes_supported=[cfg.gateway_scope],
                              resource_name="vtb mcp-gateway")


class AuditMissingBearer:
    """Records MCP requests that arrive with no bearer token at all: the
    verifier never sees those, so they would otherwise be a silent 401."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not auth.lower().startswith(b"bearer ") or not auth[7:].strip():
                audit("gateway.auth", outcome="deny", reason="missing")
        await self.app(scope, receive, send)


def build_app(gateway: Gateway, auth: RemoteAuthProvider):
    return AuditMissingBearer(build_server(gateway, auth=auth).http_app(path="/mcp"))


def create_app():
    """uvicorn --factory mcp_gateway.server:create_app"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = GatewayConfig.from_env()
    http = httpx.AsyncClient(timeout=cfg.http_timeout_s)
    routes = [Route(spec, Upstream(spec, cfg.http_timeout_s)) for spec in cfg.upstreams]
    gateway = Gateway(cfg, Hub(cfg, http), Broker(cfg, http), routes)
    return build_app(gateway, build_auth(cfg))
