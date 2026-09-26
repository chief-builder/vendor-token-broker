"""Real GitHub through the MCP gateway (markers: gateway, external). Runs
only when the gateway was started against GitHub's MCP server:

    GATEWAY_VENDOR=github GATEWAY_UPSTREAM_URL=https://api.githubcopilot.com/mcp/ \\
      docker compose -f tests/stack/docker-compose.yml --profile gateway up -d --wait mcp-gateway
    GATEWAY_VENDOR=github pytest tests/integration/test_external_github_mcp.py -m external

The GitHub App must allow the callback http://localhost:8600/v1/callback/github.
The first run skips with a link to connect GitHub once in a browser."""
import os

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from keycloak_stack import GATEWAY_MCP, mcp_token

pytestmark = [pytest.mark.gateway, pytest.mark.external]

READ_ONLY_ALLOWLIST = {"get_me", "search_repositories", "get_file_contents", "list_issues",
                       "issue_read", "list_pull_requests", "pull_request_read"}


@pytest.fixture
async def gateway():
    if os.environ.get("GATEWAY_VENDOR") != "github":
        pytest.skip("gateway not started against real GitHub (GATEWAY_VENDOR != github)")
    transport = StreamableHttpTransport(GATEWAY_MCP, headers={
        "Authorization": f"Bearer {mcp_token('alice')}"})
    async with Client(transport, timeout=60) as c:       # no elicitation: link comes back
        r = await c.call_tool("connect_github", {}, raise_on_error=False)
        if r.is_error and "/v1/authorize/github" in r.content[0].text:
            pytest.skip("connect GitHub once (sign in as alice/alice), then rerun: "
                        + r.content[0].text)
        assert not r.is_error, r.content[0].text
        yield c


async def test_real_github_tools_are_the_read_only_allowlist(gateway):
    names = {t.name for t in await gateway.list_tools()}
    assert names == READ_ONLY_ALLOWLIST | {"connect_github"}


async def test_get_me_returns_the_connected_account(gateway):
    r = await gateway.call_tool("get_me", {}, raise_on_error=False)
    assert not r.is_error, r.content[0].text
    assert '"login"' in r.content[0].text
