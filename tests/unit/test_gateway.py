"""MCP gateway behavior with fake hub, broker and upstream: the tool
catalog, forwarding, the consent flow in both protocol eras, error mapping,
and token handling."""
import json
import logging

import pytest
from gateway_helpers import (
    CONSENT_URL,
    CONSENT_URLS,
    HUB_JWT,
    MCP_TOKEN,
    VENDOR_TOKEN,
    VENDOR_TOKENS,
    HandoffError,
    ListChanged,
    Unavailable,
    client,
    make_gateway,
    upstream_tool,
)

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
        assert await _names(c) == ["connect_github", "github_get_me", "github_issue_read"]


@pytest.mark.parametrize("mode", ERAS)
async def test_snapshot_matching_upstream_changes_nothing(mode, caplog):
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    gw, *_ = make_gateway(snapshot=[upstream_tool("get_me"), upstream_tool("issue_read")],
                          tools=("get_me", "issue_read"))
    messages = ListChanged()
    async with client(gw, mode=mode, messages=messages) as c:
        await c.call_tool("connect_github", {})
    event = _catalog_events(caplog)[-1]
    assert (event["added"], event["changed"], event["missing"]) == ([], [], [])
    assert messages.count == 0


async def test_drifted_schema_is_reported_but_the_snapshot_stays_listed(caplog):
    """The list is shared by every user: one user's live schema (which a
    vendor may personalize) never replaces the snapshot."""
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    gw, *_ = make_gateway(snapshot=[upstream_tool("get_me"),
                                    upstream_tool("issue_read", param="old_param")])
    messages = ListChanged()
    async with client(gw, messages=messages) as c:
        await c.call_tool("connect_github", {})
        tool = next(t for t in await c.list_tools() if t.name == "github_issue_read")
    assert list(tool.input_schema["properties"]) == ["old_param"]
    assert _catalog_events(caplog)[-1]["changed"] == ["issue_read"]
    assert messages.count == 0


async def test_allowlisted_tool_missing_from_snapshot_is_added_and_announced(caplog):
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    gw, *_ = make_gateway(snapshot=[upstream_tool("get_me")])
    messages = ListChanged()
    async with client(gw, messages=messages) as c:
        await c.call_tool("connect_github", {})
        assert await _names(c) == ["connect_github", "github_get_me", "github_issue_read"]
    assert _catalog_events(caplog)[-1]["added"] == ["issue_read"]
    assert messages.count == 1


async def test_tool_the_vendor_dropped_stays_listed_and_is_reported(caplog):
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    gw, *_ = make_gateway(snapshot=[upstream_tool(n) for n in ("get_me", "list_issues")],
                          tools=("get_me", "list_issues"))
    async with client(gw) as c:
        await c.call_tool("connect_github", {})
        assert await _names(c) == ["connect_github", "github_get_me", "github_list_issues"]
    assert _catalog_events(caplog)[-1]["missing"] == ["list_issues"]


# ----------------------------------------------------------- no snapshot

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
        assert await _names(c) == ["connect_github", "github_get_me", "github_issue_read"]
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
        tool = next(t for t in await c.list_tools() if t.name == "github_issue_read")
    assert tool.input_schema["properties"] == {"owner": {"type": "string"}}


async def test_forwards_with_the_users_vendor_token():
    gw, hub, _, upstream = make_gateway()
    async with client(gw) as c:
        await c.call_tool("connect_github", {})
        r = await c.call_tool("github_issue_read", {"owner": "octo"})
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
    assert len(broker.resolves) == 2          # first ask + the final token fetch


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
        r = await c.call_tool("github_get_me", {}, raise_on_error=False)
    assert r.is_error and text in r.content[0].text


async def test_no_token_material_in_logs_or_results(caplog):
    caplog.set_level(logging.DEBUG)
    gw, _, broker, _ = make_gateway(connected=False)
    broker.connect_on_poll = True
    async with client(gw) as c:
        texts = [(await c.call_tool("connect_github", {})).content[0].text,
                 (await c.call_tool("github_get_me", {})).content[0].text]
    for secret in (VENDOR_TOKEN, HUB_JWT, MCP_TOKEN):
        assert secret not in caplog.text
        assert all(secret not in t for t in texts)


# ---------------------------------------------------------- several upstreams

async def test_every_upstream_is_listed_with_its_prefix():
    gw, *_ = make_gateway(with_linear=True, snapshot=[upstream_tool("get_me")],
                          tools=("get_me",))
    async with client(gw) as c:
        await c.call_tool("connect_linear", {})
        names = await _names(c)
    assert names == ["connect_github", "connect_linear", "github_get_me",
                     "linear_get_issue", "linear_list_issues"]     # save_issue stays hidden


async def test_connections_are_per_upstream():
    """Connected to GitHub but not Linear: only Linear asks to connect."""
    gw, _, broker, _ = make_gateway(connected={"github"}, with_linear=True)
    broker.connect_on_poll = True
    seen: list = []
    async with client(gw, seen=seen) as c:
        await c.call_tool("connect_github", {})
        assert seen == []
        r = await c.call_tool("connect_linear", {})
    assert "Linear is connected" in r.content[0].text
    assert [(p.url, p.message) for p in seen] == [
        (CONSENT_URLS["linear"], "Connect your Linear account to continue.")]


async def test_each_upstream_gets_only_its_own_vendors_token():
    gw, _, broker, github = make_gateway(with_linear=True)
    linear = gw.routes["linear"].client
    async with client(gw) as c:
        await c.call_tool("connect_github", {})
        await c.call_tool("connect_linear", {})
        await c.call_tool("github_get_me", {})
        await c.call_tool("linear_list_issues", {"owner": "x"})
    assert [(t, n) for t, n, _ in github.calls] == [(VENDOR_TOKENS["github"], "get_me")]
    assert [(t, n) for t, n, _ in linear.calls] == [(VENDOR_TOKENS["linear"], "list_issues")]
    assert broker.resolves.count("linear") == 2 and broker.resolves.count("github") == 2


async def test_catalogs_load_per_upstream():
    gw, _, _, github = make_gateway(with_linear=True)
    async with client(gw) as c:
        await c.call_tool("connect_github", {})
        assert gw.routes["linear"].client.lists == 0
        await c.call_tool("connect_linear", {})
    assert github.lists == 1 and gw.routes["linear"].client.lists == 1


async def test_errors_name_the_right_service():
    gw, *_ = make_gateway(connected={"github"}, with_linear=True)
    async with client(gw, action=None) as c:
        r = await c.call_tool("connect_linear", {}, raise_on_error=False)
    assert r.is_error and r.content[0].text.startswith("Connect Linear first")
    assert CONSENT_URLS["linear"] in r.content[0].text


async def test_upstream_error_detail_is_logged_without_the_token(caplog):
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    gw, _, _, upstream = make_gateway()
    upstream.error = RuntimeError(f"server said no to {VENDOR_TOKEN}")
    async with client(gw) as c:
        await c.call_tool("connect_github", {})
        await c.call_tool("github_get_me", {}, raise_on_error=False)
    event = [json.loads(m) for m in caplog.messages if '"upstream-error"' in m][-1]
    assert event["detail"] == "server said no to <token>"
    assert VENDOR_TOKEN not in caplog.text
