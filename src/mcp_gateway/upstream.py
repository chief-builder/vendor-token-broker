"""The vendor's MCP server. Every operation opens a fresh session with the
calling user's vendor token and closes it: nothing is kept between calls.
GitHub negotiates at most 2025-11-25, so the handshake era is pinned."""
from typing import Any

import mcp_types
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from .config import GatewayConfig


class Upstream:
    def __init__(self, cfg: GatewayConfig):
        self._cfg = cfg

    def _headers(self, token: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}"}
        if self._cfg.upstream_readonly:
            headers["X-MCP-Readonly"] = "true"
        if self._cfg.upstream_lockdown:
            headers["X-MCP-Lockdown"] = "true"
        if self._cfg.upstream_toolsets:
            headers["X-MCP-Toolsets"] = ",".join(self._cfg.upstream_toolsets)
        return headers

    def _client(self, token: str) -> Client:
        return Client(StreamableHttpTransport(self._cfg.upstream_url,
                                              headers=self._headers(token)),
                      mode="legacy", timeout=self._cfg.http_timeout_s)

    async def list_tools(self, token: str) -> list[mcp_types.Tool]:
        async with self._client(token) as up:
            return list((await up.list_tools_mcp()).tools)

    async def call_tool(self, token: str, name: str,
                        arguments: dict[str, Any]) -> mcp_types.CallToolResult:
        async with self._client(token) as up:
            return await up.call_tool_mcp(name, arguments)
