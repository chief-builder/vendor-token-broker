"""MCP gateway behavior with fake hub, broker and upstream: the tool
catalog, forwarding, the consent flow in both protocol eras, error mapping,
and token handling."""
import json
import logging

import pytest
from gateway_helpers import (
    CONSENT_URL,
    HUB_JWT,
    MCP_TOKEN,
    VENDOR_TOKEN,
    HandoffError,
    ListChanged,
    Unavailable,
    client,
    make_gateway,
    upstream_tool,
)

from mcp_gateway.config import DEFAULT_TOOLS
from mcp_gateway.server import load_snapshot

ERAS = ["legacy", "2026-07-28"]


async def _names(c) -> list[str]:
    return sorted(t.name for t in await c.list_tools())


def _catalog_events(caplog) -> list[dict]:
    return [json.loads(m) for m in caplog.messages if '"gateway.catalog"' in m]


# ------------------------------------------------ pinned tool list (snapshot)

async def test_snapshot_tools_are_listed_before_anyone_connects():
    gw, *_ = make_gateway(snapshot=[upstream_tool(n) for n in
                                    ("get_me", "issue_read", "create_issue")])
    async with client(gw) as c:
        # create_issue is in the snapshot but not allowlisted.
        assert await _names(c) == ["connect_github", "get_me", "issue_read"]


@pytest.mark.parametrize("mode", ERAS)
async def test_snapshot_matching_upstream_changes_nothing(mode, caplog):
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    gw, *_ = make_gateway(snapshot=[upstream_tool("get_me"), upstream_tool("issue_read")],
                          upstream_tools=("get_me", "issue_read"))
    messages = ListChanged()
    async with client(gw, mode=mode, messages=messages) as c:
        await c.call_tool("connect_github", {})
    event = _catalog_events(caplog)[-1]
    assert (event["added"], event["changed"], event["missing"]) == ([], [], [])
    assert messages.count == 0


async def test_drifted_schema_is_replaced_by_the_live_one(caplog):
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    gw, *_ = make_gateway(snapshot=[upstream_tool("get_me"),
                                    upstream_tool("issue_read", param="old_param")])
    messages = ListChanged()
    async with client(gw, messages=messages) as c:
        await c.call_tool("connect_github", {})
        tool = next(t for t in await c.list_tools() if t.name == "issue_read")
    assert list(tool.input_schema["properties"]) == ["owner"]
    assert _catalog_events(caplog)[-1]["changed"] == ["issue_read"]
    assert messages.count == 0                    # same names: nothing to announce


async def test_allowlisted_tool_missing_from_snapshot_is_added_and_announced(caplog):
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    gw, *_ = make_gateway(snapshot=[upstream_tool("get_me")])
    messages = ListChanged()
    async with client(gw, messages=messages) as c:
        await c.call_tool("connect_github", {})
        assert await _names(c) == ["connect_github", "get_me", "issue_read"]
    assert _catalog_events(caplog)[-1]["added"] == ["issue_read"]
    assert messages.count == 1


async def test_tool_the_vendor_dropped_stays_listed_and_is_reported(caplog):
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    gw, *_ = make_gateway(snapshot=[upstream_tool(n) for n in ("get_me", "list_issues")],
                          upstream_tools=("get_me", "list_issues"))
    async with client(gw) as c:
        await c.call_tool("connect_github", {})
        assert await _names(c) == ["connect_github", "get_me", "list_issues"]
    assert _catalog_events(caplog)[-1]["missing"] == ["list_issues"]


def test_bundled_snapshot_is_exactly_the_default_allowlist():
    tools = load_snapshot()
    assert [t.name for t in tools] == list(DEFAULT_TOOLS)
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)
    assert load_snapshot("none") == []


# ------------------------------------------- no snapshot (UPSTREAM_TOOL_SNAPSHOT=none)

async def test_starts_with_connect_github_only():
    gw, *_ = make_gateway()
    async with client(gw) as c:
        assert await _names(c) == ["connect_github"]


@pytest.mark.parametrize("mode", ERAS)
async def test_connect_loads_allowlisted_tools_and_announces_them(mode):
    gw, _, _, upstream = make_gateway()
    messages = ListChanged()
    async with client(gw, mode=mode, messages=messages) as c:
        r = await c.call_tool("connect_github", {})
        assert "2 GitHub tools" in r.content[0].text
        # create_issue exists upstream but is not allowlisted.
        assert await _names(c) == ["connect_github", "get_me", "issue_read"]
    assert messages.count == 1
    assert upstream.lists == 1


async def test_catalog_loads_once():
    gw, _, _, upstream = make_gateway()
    async with client(gw) as c:
        await c.call_tool("connect_github", {})
        await c.call_tool("connect_github", {})
    assert upstream.lists == 1


async def test_upstream_schema_is_exposed():
    gw, *_ = make_gateway()
    async with client(gw) as c:
        await c.call_tool("connect_github", {})
        tool = next(t for t in await c.list_tools() if t.name == "issue_read")
    assert tool.input_schema["properties"] == {"owner": {"type": "string"}}


async def test_forwards_with_the_users_vendor_token():
    gw, hub, _, upstream = make_gateway()
    async with client(gw) as c:
        await c.call_tool("connect_github", {})
        r = await c.call_tool("issue_read", {"owner": "octo"})
    assert r.content[0].text == "issue_read ok {'owner': 'octo'}"
    assert upstream.calls == [(VENDOR_TOKEN, "issue_read", {"owner": "octo"})]
    assert hub.seen[-1] == MCP_TOKEN          # the MCP token only ever goes to the hub


@pytest.mark.parametrize("mode", ERAS)
async def test_not_connected_elicits_the_consent_link_then_proceeds(mode):
    gw, _, broker, _ = make_gateway(connected=False)
    broker.connect_on_poll = True             # user finishes in the browser
    seen: list = []
    async with client(gw, mode=mode, seen=seen) as c:
        r = await c.call_tool("connect_github", {})
        assert c.protocol_version == ("2025-11-25" if mode == "legacy" else "2026-07-28")
    assert "GitHub is connected" in r.content[0].text
    assert [(p.mode, p.url) for p in seen] == [("url", CONSENT_URL)]


@pytest.mark.parametrize("mode", ERAS)
@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_declined_consent_is_an_error(mode, action):
    gw, _, broker, _ = make_gateway(connected=False)
    async with client(gw, mode=mode, action=action) as c:
        r = await c.call_tool("connect_github", {}, raise_on_error=False)
    assert r.is_error and "not connected" in r.content[0].text
    assert broker.polls == 0


@pytest.mark.parametrize("mode", ERAS)
async def test_client_without_url_elicitation_gets_the_link(mode):
    gw, *_ = make_gateway(connected=False)
    async with client(gw, mode=mode, action=None) as c:
        r = await c.call_tool("connect_github", {}, raise_on_error=False)
    assert r.is_error and CONSENT_URL in r.content[0].text


async def test_accepted_but_never_finished_times_out():
    gw, _, broker, _ = make_gateway(connected=False, consent_wait_s=0)
    async with client(gw) as c:
        r = await c.call_tool("connect_github", {}, raise_on_error=False)
    assert r.is_error and "not connected yet" in r.content[0].text


async def test_waiting_polls_grants_not_resolve():
    """Polling resolve would mint a new consent link on every miss."""
    gw, _, broker, _ = make_gateway(connected=False)
    broker.connect_on_poll = True
    async with client(gw) as c:
        await c.call_tool("connect_github", {})
    assert broker.resolves == 2               # first ask + the final token fetch


@pytest.mark.parametrize("error, text", [
    (Unavailable("broker answered 503 vault-unavailable"), "retry shortly"),
    (HandoffError("broker rejected the hub token (invalid-hub-token)"),
     "gateway configuration problem"),
])
async def test_broker_failures_never_ask_to_connect(error, text):
    gw, _, broker, _ = make_gateway()
    broker.error = error
    seen: list = []
    async with client(gw, seen=seen) as c:
        r = await c.call_tool("connect_github", {}, raise_on_error=False)
    assert r.is_error and text in r.content[0].text
    assert seen == []


async def test_hub_refusing_the_exchange_is_a_gateway_problem():
    gw, hub, *_ = make_gateway()
    hub.error = HandoffError("hub refused token exchange (400 invalid_request)")
    async with client(gw) as c:
        r = await c.call_tool("connect_github", {}, raise_on_error=False)
    assert r.is_error and "gateway configuration problem" in r.content[0].text


async def test_revoke_pending_is_reported_not_reconnected():
    gw, _, broker, _ = make_gateway()
    broker.problem = "revoke-pending"
    seen: list = []
    async with client(gw, seen=seen) as c:
        r = await c.call_tool("connect_github", {}, raise_on_error=False)
    assert r.is_error and "revoke-pending" in r.content[0].text
    assert seen == []


@pytest.mark.parametrize("error, text", [
    (RuntimeError("Client error '401 Unauthorized'"), "reconnect GitHub"),
    (ConnectionError("boom"), "unavailable"),
])
async def test_upstream_failures(error, text):
    gw, _, _, upstream = make_gateway()
    async with client(gw) as c:
        await c.call_tool("connect_github", {})
        upstream.error = error
        r = await c.call_tool("get_me", {}, raise_on_error=False)
    assert r.is_error and text in r.content[0].text


async def test_no_token_material_in_logs_or_results(caplog):
    caplog.set_level(logging.DEBUG)
    gw, _, broker, _ = make_gateway(connected=False)
    broker.connect_on_poll = True
    async with client(gw) as c:
        texts = [(await c.call_tool("connect_github", {})).content[0].text,
                 (await c.call_tool("get_me", {})).content[0].text]
    for secret in (VENDOR_TOKEN, HUB_JWT, MCP_TOKEN):
        assert secret not in caplog.text
        assert all(secret not in t for t in texts)
