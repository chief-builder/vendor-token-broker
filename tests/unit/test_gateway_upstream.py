"""Upstream against a real local HTTP server: an MCP server that answers
401 surfaces as UpstreamRejected (the MCP SDK itself reports only a generic
protocol error), and other failures are not mistaken for it."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mcp_gateway.config import UpstreamSpec
from mcp_gateway.upstream import Upstream, UpstreamRejected


def serve(status: int):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_DELETE = do_POST

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def upstream_at():
    servers = []

    def make(status: int) -> Upstream:
        server = serve(status)
        servers.append(server)
        url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
        return Upstream(
            UpstreamSpec(name="x", display_name="X", vendor="x", url=url, tools=("t",)), 5
        )

    yield make
    for server in servers:
        server.shutdown()


async def test_401_is_upstream_rejected(upstream_at):
    with pytest.raises(UpstreamRejected) as err:
        await upstream_at(401).call_tool("vendor-secret-token", "t", {})
    assert "vendor-secret-token" not in str(err.value)


async def test_401_on_listing_is_upstream_rejected(upstream_at):
    with pytest.raises(UpstreamRejected):
        await upstream_at(401).list_tools("vendor-secret-token")


@pytest.mark.parametrize("status", [403, 500, 503])
async def test_other_failures_are_not_rejections(upstream_at, status):
    with pytest.raises(Exception) as err:
        await upstream_at(status).call_tool("vendor-secret-token", "t", {})
    assert not isinstance(err.value, UpstreamRejected)
