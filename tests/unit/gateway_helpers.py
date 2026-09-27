"""Fakes for the MCP gateway's dependencies (hub, broker, upstream MCP
servers). Uniquely named so bare imports never collide with the
integration suite's modules."""
from dataclasses import dataclass, field
from typing import Any

import mcp_types
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.messages import MessageHandler

from mcp_gateway.clients import HandoffError, Resolved, Unavailable
from mcp_gateway.config import GatewayConfig, UpstreamSpec
from mcp_gateway.server import Gateway, Route, build_server

VENDOR_TOKENS = {"github": "ghu_FAKEvendorTOKEN000000000000000000000",
                 "linear": "lin_oauth_FAKEvendorTOKEN0000000000000000"}
VENDOR_TOKEN = VENDOR_TOKENS["github"]
HUB_JWT = "hub.jwt.for-alice"
MCP_TOKEN = "mcp.token.for-alice"
CONSENT_URLS = {v: f"http://localhost:8600/v1/authorize/{v}?txn=t1" for v in VENDOR_TOKENS}
CONSENT_URL = CONSENT_URLS["github"]
DISPLAY = {"github": "GitHub", "linear": "Linear"}


def make_gateway_config(upstreams: tuple[UpstreamSpec, ...] = (), **over) -> GatewayConfig:
    base = dict(public_url="http://localhost:8500", hub_issuer="http://hub.test/realms/mcp",
                hub_jwks_uri="http://hub.test/certs", hub_token_endpoint="http://hub.test/token",
                client_id="mcp-gateway", client_secret="s", broker_url="http://broker.test",
                upstreams=upstreams, consent_wait_s=5)
    base.update(over)
    return GatewayConfig(**base)


class FakeHub:
    def __init__(self):
        self.error: Exception | None = None
        self.seen: list[str] = []

    async def exchange(self, mcp_token: str) -> str:
        self.seen.append(mcp_token)
        if self.error:
            raise self.error
        return HUB_JWT


class FakeBroker:
    """Which vendors the user has connected; `connect_on_poll` simulates the
    user finishing the browser flow while the gateway waits."""

    def __init__(self, connected: set[str]):
        self.connected_vendors = set(connected)
        self.connect_on_poll = False
        self.problem = ""
        self.error: Exception | None = None
        self.poll_error: Exception | None = None      # raised while waiting on consent
        self.resolves: list[str] = []
        self.polls = 0

    @property
    def is_connected(self) -> bool:          # single-vendor shorthand used by tests
        return "github" in self.connected_vendors

    async def resolve(self, hub_jwt: str, vendor: str) -> Resolved:
        assert hub_jwt == HUB_JWT
        self.resolves.append(vendor)
        if self.error:
            raise self.error
        if self.problem:
            return Resolved(problem=self.problem)
        if vendor in self.connected_vendors:
            return Resolved(token=VENDOR_TOKENS[vendor])
        return Resolved(consent_url=CONSENT_URLS[vendor], problem="needs-consent")

    async def connected(self, hub_jwt: str, vendor: str) -> bool:
        self.polls += 1
        if self.poll_error:
            raise self.poll_error
        if self.connect_on_poll:
            self.connected_vendors.add(vendor)
        return vendor in self.connected_vendors


def upstream_tool(name: str, param: str = "owner") -> mcp_types.Tool:
    return mcp_types.Tool(name=name, description=f"upstream {name}", input_schema={
        "type": "object", "properties": {param: {"type": "string"}}})


@dataclass
class FakeUpstream:
    vendor: str = "github"
    names: tuple[str, ...] = ("get_me", "issue_read", "create_issue")
    error: Exception | None = None
    calls: list[tuple[str, str, dict]] = field(default_factory=list)
    lists: int = 0

    async def list_tools(self, token: str) -> list[mcp_types.Tool]:
        assert token == VENDOR_TOKENS[self.vendor]
        self.lists += 1
        return [upstream_tool(n) for n in self.names]

    async def call_tool(self, token: str, name: str, arguments: dict[str, Any]
                        ) -> mcp_types.CallToolResult:
        if self.error:
            raise self.error
        self.calls.append((token, name, arguments))
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=f"{name} ok {arguments}")])


def spec(name: str = "github", tools: tuple[str, ...] = ("get_me", "issue_read"),
         snapshot: list[mcp_types.Tool] | None = None) -> UpstreamSpec:
    return UpstreamSpec(name=name, display_name=DISPLAY[name], vendor=name,
                        url=f"http://{name}.test/mcp", tools=tools,
                        snapshot=tuple(snapshot or ()))


def make_gateway(connected: bool | set[str] = True,
                 snapshot: list[mcp_types.Tool] | None = None,
                 tools: tuple[str, ...] = ("get_me", "issue_read"),
                 with_linear: bool = False, **cfg
                 ) -> tuple[Gateway, FakeHub, FakeBroker, FakeUpstream]:
    """A gateway in front of a fake GitHub (and optionally a fake Linear).
    No snapshot by default: tools appear on the first connected call.
    Returns the GitHub fake upstream; `gw.routes["linear"].client` is Linear's."""
    if isinstance(connected, bool):
        connected = {"github", "linear"} if connected else set()
    hub, broker = FakeHub(), FakeBroker(connected)
    github = FakeUpstream("github")
    routes = [Route(spec("github", tools, snapshot), github)]
    if with_linear:
        routes.append(Route(spec("linear", ("list_issues", "get_issue")),
                            FakeUpstream("linear", ("list_issues", "get_issue", "save_issue"))))
    gw = Gateway(make_gateway_config(tuple(r.spec for r in routes), **cfg), hub, broker, routes,
                 current_token=lambda: (MCP_TOKEN, "alice"))
    build_server(gw)
    return gw, hub, broker, github


class ListChanged(MessageHandler):
    def __init__(self):
        super().__init__()
        self.count = 0

    async def on_tool_list_changed(self, message) -> None:
        self.count += 1


def answering(action: str | None, seen: list):
    """An elicitation handler that records the request and answers `action`."""
    async def handler(message, response_type, params, ctx):
        seen.append(params)
        return ElicitResult(action=action)
    return handler


def client(gw: Gateway, mode: str = "legacy", action: str | None = "accept",
           seen: list | None = None, messages: MessageHandler | None = None) -> Client:
    kw: dict = {"mode": mode}
    if action is not None:
        kw["elicitation_handler"] = answering(action, seen if seen is not None else [])
    if messages is not None:
        kw["message_handler"] = messages
    return Client(gw.mcp, **kw)


__all__ = ["HandoffError", "Unavailable"]
