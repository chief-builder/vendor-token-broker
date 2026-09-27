"""RFC 8707 resource indicators for vendors whose tokens are bound to an MCP
server (their own MCP authorization server, e.g. Atlassian, Cloudflare): the
MCP authorization spec requires `resource` on the authorization request, the
code exchange, and every refresh (the latter two: test_vendor_client.py).
Ordinary vendor APIs never get one."""
from broker_harness import Harness, query

MCP = "https://mcp.example.test/mcp"


async def _vendor_redirect(h: Harness) -> dict:
    r = await h.hub_callback(await h.authorize())
    assert r.status_code in (302, 307), r.text
    return query(r.headers["location"])


async def test_authorize_sends_resource_when_the_vendor_has_one():
    h = Harness()
    h.vendors.spec["resource"] = MCP
    assert (await _vendor_redirect(h))["resource"] == MCP


async def test_authorize_sends_no_resource_for_ordinary_vendors():
    assert "resource" not in await _vendor_redirect(Harness())
