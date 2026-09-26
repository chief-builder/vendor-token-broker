"""Fakes for the MCP gateway's dependencies (hub, broker, upstream MCP
server). Uniquely named so bare imports never collide with the
integration suite's modules."""
from dataclasses import dataclass, field
from typing import Any

import mcp_types
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.messages import MessageHandler

from mcp_gateway.clients import HandoffError, Resolved, Unavailable
from mcp_gateway.config import GatewayConfig
from mcp_gateway.server import Gateway, build_server

VENDOR_TOKEN = "ghu_FAKEvendorTOKEN000000000000000000000"
HUB_JWT = "hub.jwt.for-alice"
MCP_TOKEN = "mcp.token.for-alice"
CONSENT_URL = "http://localhost:8600/v1/authorize/github?txn=t1"


def make_gateway_config(**over) -> GatewayConfig:
    base = dict(public_url="http://localhost:8500", hub_issuer="http://hub.test/realms/mcp",
                hub_jwks_uri="http://hub.test/certs", hub_token_endpoint="http://hub.test/token",
                client_id="mcp-gateway", client_secret="s", broker_url="http://broker.test",
                consent_wait_s=5)
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
    """Connected or not; `connect_on_poll` simulates the user finishing the
    browser flow while the gateway waits."""

    def __init__(self, connected: bool = True):
        self.is_connected = connected
        self.connect_on_poll = False
        self.problem = ""
        self.error: Exception | None = None
        self.resolves = 0
        self.polls = 0

    async def resolve(self, hub_jwt: str) -> Resolved:
        assert hub_jwt == HUB_JWT
        self.resolves += 1
        if self.error:
            raise self.error
        if self.problem:
            return Resolved(problem=self.problem)
        if self.is_connected:
            return Resolved(token=VENDOR_TOKEN)
        return Resolved(consent_url=CONSENT_URL, problem="needs-consent")

    async def connected(self, hub_jwt: str) -> bool:
        self.polls += 1
        if self.connect_on_poll:
            self.is_connected = True
        return self.is_connected


def _tool(name: str) -> mcp_types.Tool:
    return mcp_types.Tool(name=name, description=f"upstream {name}", input_schema={
        "type": "object", "properties": {"owner": {"type": "string"}}})


@dataclass
class FakeUpstream:
    names: tuple[str, ...] = ("get_me", "issue_read", "create_issue")
    error: Exception | None = None
    calls: list[tuple[str, str, dict]] = field(default_factory=list)
    lists: int = 0

    async def list_tools(self, token: str) -> list[mcp_types.Tool]:
        assert token == VENDOR_TOKEN
        self.lists += 1
        return [_tool(n) for n in self.names]

    async def call_tool(self, token: str, name: str, arguments: dict[str, Any]
                        ) -> mcp_types.CallToolResult:
        if self.error:
            raise self.error
        self.calls.append((token, name, arguments))
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=f"{name} ok {arguments}")])


def make_gateway(connected: bool = True, **cfg) -> tuple[Gateway, FakeHub, FakeBroker,
                                                         FakeUpstream]:
    hub, broker, upstream = FakeHub(), FakeBroker(connected), FakeUpstream()
    gw = Gateway(make_gateway_config(**cfg), hub, broker, upstream,
                 current_token=lambda: (MCP_TOKEN, "alice"))
    build_server(gw)
    return gw, hub, broker, upstream


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
