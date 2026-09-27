"""Real vendor MCP servers through the gateway (markers: gateway, external).
Runs only when the gateway serves the bundled upstreams (the real servers):

    GATEWAY_UPSTREAMS= GATEWAY_CONSENT_WAIT_S=120 \\
      docker compose -f tests/stack/docker-compose.yml --profile gateway \\
      up -d --no-deps --wait mcp-gateway
    pytest tests/integration/test_external_mcp_servers.py -m external

Each server needs its OAuth app configured on the stack (GITHUB_CLIENT_ID /
LINEAR_CLIENT_ID with secrets in tests/stack/.env, callback
http://localhost:8600/v1/callback/<vendor>). The first run skips each server
with a link to connect it once in a browser (sign in as alice/alice)."""
import subprocess

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from keycloak_stack import GATEWAY_MCP, mcp_token

pytestmark = [pytest.mark.gateway, pytest.mark.external]

SERVERS = {
    "github": ("get_me", {}, '"login"'),
    "linear": ("list_teams", {}, '"teams"'),
}


def _real_upstreams() -> bool:
    out = subprocess.run(["docker", "exec", "vtb-mcp-gateway", "printenv", "GATEWAY_UPSTREAMS"],
                         capture_output=True, text=True)
    return out.returncode == 0 and out.stdout.strip() == ""


@pytest.fixture(scope="module")
def bundled_tools() -> dict[str, list[str]]:
    from mcp_gateway.config import load_upstreams
    return {u.name: list(u.tools) for u in load_upstreams()}


@pytest.mark.parametrize("server", sorted(SERVERS))
async def test_real_server_read_only_tools_work(server, bundled_tools):
    if not _real_upstreams():
        pytest.skip("gateway is not serving the real upstreams (GATEWAY_UPSTREAMS is set)")
    transport = StreamableHttpTransport(GATEWAY_MCP, headers={
        "Authorization": f"Bearer {mcp_token('alice')}"})
    async with Client(transport, timeout=60) as c:          # no elicitation: link comes back
        r = await c.call_tool(f"connect_{server}", {}, raise_on_error=False)
        if r.is_error and f"/v1/authorize/{server}" in r.content[0].text:
            pytest.skip(f"connect {server} once (sign in as alice/alice), then rerun: "
                        + r.content[0].text)
        assert not r.is_error, r.content[0].text
        names = {t.name for t in await c.list_tools()}
        assert {f"{server}_{t}" for t in bundled_tools[server]} <= names
        tool, args, expect = SERVERS[server]
        r = await c.call_tool(f"{server}_{tool}", args, raise_on_error=False)
    assert not r.is_error, r.content[0].text
    assert expect in "".join(getattr(b, "text", "") for b in r.content)
