"""A vendor's MCP server. Every operation opens a fresh session with the
calling user's vendor token and closes it: nothing is kept between calls.
The protocol era is set per upstream (GitHub and Linear negotiate at most
2025-11-25, so they pin the handshake era)."""

from typing import Any

import mcp_types
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from .config import UpstreamSpec


class Upstream:
    def __init__(self, spec: UpstreamSpec, timeout_s: float):
        self.spec, self._timeout = spec, timeout_s

    def _client(self, token: str) -> Client:
        headers = {**self.spec.headers, "Authorization": f"{self.spec.auth_scheme} {token}"}
        return Client(
            StreamableHttpTransport(self.spec.url, headers=headers),
            mode=self.spec.protocol,
            timeout=self._timeout,
        )

    async def list_tools(self, token: str) -> list[mcp_types.Tool]:
        async with self._client(token) as up:
            return list((await up.list_tools_mcp()).tools)

    async def call_tool(
        self, token: str, name: str, arguments: dict[str, Any]
    ) -> mcp_types.CallToolResult:
        async with self._client(token) as up:
            return await up.call_tool_mcp(name, arguments)
