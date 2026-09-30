"""A vendor's MCP server. Every operation opens a fresh session with the
calling user's vendor token and closes it: nothing is kept between calls.
The protocol era is set per upstream (GitHub and Linear negotiate at most
2025-11-25, so they pin the handshake era)."""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import mcp_types
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

# The SDK's default client (MCP timeouts); FastMCP 4.0.10 imports it from here too.
from mcp.shared._httpx_utils import create_mcp_http_client

from .config import UpstreamSpec


class UpstreamRejected(Exception):
    """The vendor's MCP server answered 401: it no longer accepts the token
    (revoked or expired at the vendor), so the user must reconnect."""


def _recording_client_factory(statuses: list[int]):
    """An HTTP client factory for the MCP transport that records every
    response status. The MCP SDK reports an HTTP 401 only as a generic
    protocol error, so the status is the one reliable signal."""

    async def record(response: httpx2.Response) -> None:
        statuses.append(response.status_code)

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        **_: Any,  # e.g. follow_redirects: keep the SDK's default client behavior
    ) -> httpx2.AsyncClient:
        client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
        client.event_hooks["response"].append(record)
        return client

    return factory


class Upstream:
    def __init__(self, spec: UpstreamSpec, timeout_s: float):
        self.spec, self._timeout = spec, timeout_s

    def _client(self, token: str, statuses: list[int]) -> Client:
        headers = {**self.spec.headers, "Authorization": f"{self.spec.auth_scheme} {token}"}
        transport = StreamableHttpTransport(
            self.spec.url,
            headers=headers,
            httpx_client_factory=_recording_client_factory(statuses),
        )
        return Client(transport, mode=self.spec.protocol, timeout=self._timeout)

    async def _run[T](self, token: str, op: Callable[[Client], Awaitable[T]]) -> T:
        statuses: list[int] = []
        try:
            async with self._client(token, statuses) as up:
                return await op(up)
        except Exception as exc:
            if 401 in statuses:
                raise UpstreamRejected(f"{self.spec.name} answered 401") from exc
            raise

    async def list_tools(self, token: str) -> list[mcp_types.Tool]:
        async def op(up: Client) -> list[mcp_types.Tool]:
            return list((await up.list_tools_mcp()).tools)

        return await self._run(token, op)

    async def call_tool(
        self, token: str, name: str, arguments: dict[str, Any]
    ) -> mcp_types.CallToolResult:
        async def op(up: Client) -> mcp_types.CallToolResult:
            return await up.call_tool_mcp(name, arguments)

        return await self._run(token, op)
