"""End to end through the MCP gateway (gateway profile): a real MCP client
with a Keycloak-issued MCP token calls the gateway, which exchanges it at
Keycloak, resolves the user's vendor token at broker-kc, and forwards to the
vendor stand-ins (mock-mcp: /github/mcp for vendor mockhub, /linear/mcp for
vendor mockhub-jwt, /atlassian/mcp and /cloudflare/mcp for vendors
mockhub-atlassian and mockhub-cloudflare, whose tokens are bound to their
server; all accept only live mock-vendor tokens).
When the user isn't connected, the client's elicitation handler plays the
user: it opens the consent link and signs in at Keycloak."""
import asyncio
import contextlib
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import requests
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import StreamableHttpTransport
from keycloak_stack import (
    BROKER_KC,
    GATEWAY_MCP,
    MOCK_MCP,
    consent_via_keycloak,
    hub_jwt,
    mcp_token,
    resolve_kc,
    revoke_kc,
)
from stack import grep_container_logs, mock_state

pytestmark = pytest.mark.gateway

GITHUB = {"github_" + t for t in (
    "get_me", "search_repositories", "get_file_contents", "list_issues", "issue_read",
    "list_pull_requests", "pull_request_read")}
LINEAR = {"linear_" + t for t in (
    "list_issues", "get_issue", "list_comments", "list_projects", "get_project", "list_cycles",
    "list_teams", "get_team", "list_issue_statuses", "list_users", "get_user",
    "list_documents", "get_document")}
ATLASSIAN = {"atlassian_" + t for t in (
    "getJiraIssue", "searchJiraIssuesUsingJql", "getConfluenceContent", "searchConfluence",
    "executeRead", "discover", "atlassianUserInfo", "getAccessibleAtlassianResources")}
# Atlassian and Cloudflare have no snapshot yet: their tools are listed once
# someone connects (the catalog load), so only their connect tools are here.
ALL_TOOLS = GITHUB | LINEAR | {"connect_github", "connect_linear", "connect_atlassian",
                               "connect_cloudflare"}
OWN_SIGN_IN = ("mockhub-atlassian", "mockhub-cloudflare")
STAND_IN = "/config/stand-in-upstreams.json"


def _wait_healthy(url: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with contextlib.suppress(requests.RequestException):
            if requests.get(url, timeout=2).status_code == 200:
                return
        time.sleep(1)
    raise AssertionError(f"{url} not healthy")


@pytest.fixture(scope="module", autouse=True)
def fresh_gateway():
    """Start from a gateway that has never loaded the catalog, and users
    who have never connected."""
    upstreams = subprocess.run(
        ["docker", "exec", "vtb-mcp-gateway", "printenv", "GATEWAY_UPSTREAMS"],
        capture_output=True, text=True).stdout.strip()
    if upstreams != STAND_IN:
        pytest.skip(f"gateway uses upstreams {upstreams or '(bundled: real servers)'!r}, "
                    "not the stand-ins; recreate it without GATEWAY_UPSTREAMS")
    subprocess.run(["docker", "restart", "vtb-mcp-gateway"], check=True, capture_output=True)
    _wait_healthy("http://localhost:8500/.well-known/oauth-protected-resource/mcp")
    for user in ("alice", "bob"):
        for vendor in ("mockhub", "mockhub-jwt", *OWN_SIGN_IN):
            revoke_kc(hub_jwt(user), vendor)
    requests.post(f"{MOCK_MCP}/_test/reset", timeout=5)


class User:
    """Plays the person at the MCP client: answers the connect prompt."""

    def __init__(self, username: str, action: str = "accept", sign_in_as: str | None = None):
        self.username, self.action = username, action
        self.sign_in_as = sign_in_as or username
        self.prompts: list = []
        self.pages: list[int] = []

    async def __call__(self, message, response_type, params, ctx):
        self.prompts.append(params)
        if self.action == "accept":
            page = await asyncio.to_thread(consent_via_keycloak, params.url, self.sign_in_as)
            self.pages.append(page.status_code)
        return ElicitResult(action=self.action)


def gateway_client(username: str, user: User | None = None, mode: str = "legacy",
                   token: str | None = None) -> Client:
    transport = StreamableHttpTransport(GATEWAY_MCP, headers={
        "Authorization": f"Bearer {token or mcp_token(username)}"})
    kw = {"elicitation_handler": user} if user else {}
    return Client(transport, mode=mode, timeout=60, **kw)


def _last_call(upstream: str) -> dict:
    calls = requests.get(f"{MOCK_MCP}/_test/state", timeout=5).json()["calls"]
    return [c for c in calls if c["upstream"] == upstream][-1]


async def _call(username: str, tool: str, args: dict | None = None, **kw):
    async with gateway_client(username, **kw) as c:
        return await c.call_tool(tool, args or {}, raise_on_error=False)


# ------------------------------------------------------------- first contact

async def test_all_tools_are_listed_before_anyone_connects():
    """Pinned tool list: no client ever needs a list-changed notification."""
    async with gateway_client("alice") as c:
        assert {t.name for t in await c.list_tools()} == ALL_TOOLS


@pytest.mark.parametrize("username, mode", [("alice", "legacy"), ("bob", "2026-07-28")])
async def test_connect_runs_consent_then_lists_the_allowlisted_tools(username, mode):
    user = User(username)
    async with gateway_client(username, user, mode=mode) as c:
        r = await c.call_tool("connect_github", {}, raise_on_error=False)
        names = {t.name for t in await c.list_tools()}
    assert not r.is_error, r.content[0].text
    assert "GitHub is connected" in r.content[0].text
    assert [p.url.split("?")[0] for p in user.prompts] == [f"{BROKER_KC}/v1/authorize/mockhub"]
    assert user.pages == [200]                           # "Connected" page in the browser
    assert names == ALL_TOOLS              # create_issue and save_issue stay hidden


# ------------------------------------------------------------ connected use

@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_tool_call_reaches_github_with_the_users_token_and_policy(mode):
    r = await _call("alice", "github_issue_read",
                    {"owner": "octocat-lab", "repo": "hello-world", "issue_number": 1},
                    user=User("alice", action="decline"), mode=mode)
    assert not r.is_error, r.content[0].text
    assert "First issue" in r.content[0].text
    call = _last_call("github")
    assert call["tool"] == "issue_read" and call["args"]["issue_number"] == 1
    assert call["scheme"] == "Bearer"
    assert (call["readonly"], call["lockdown"]) == ("true", "true")
    assert call["toolsets"] == "repos,issues,pull_requests,context"


async def test_a_connected_user_is_never_prompted():
    user = User("alice", action="decline")
    r = await _call("alice", "github_get_me", user=user)
    assert not r.is_error and "octocat-lab" in r.content[0].text
    assert user.prompts == []


async def test_parallel_calls_never_burn_the_rotating_token_family():
    """Every call refreshes (mock tokens live 60s): single-flight at the
    broker must keep the rotating refresh-token family intact."""
    before = mock_state()["counters"]

    async def one():
        return await _call("alice", "github_get_me", token=token)

    token = mcp_token("alice")
    results = await asyncio.gather(*[one() for _ in range(10)])
    assert all(not r.is_error for r in results), [r.content[0].text for r in results]
    assert mock_state()["counters"]["rt_replay"] == before["rt_replay"]


# ------------------------------------------------------ a second upstream

async def test_linear_connects_separately_from_github():
    """Connected to GitHub does not mean connected to Linear: a Linear tool
    asks for its own connection, and GitHub keeps working without a prompt."""
    await _call("alice", "github_get_me", user=User("alice"))   # GitHub connected
    revoke_kc(hub_jwt("alice"), "mockhub-jwt")
    user = User("alice")
    r = await _call("alice", "linear_list_issues", user=user)
    assert not r.is_error, r.content[0].text
    assert "Fix login" in r.content[0].text
    assert [p.url.split("?")[0] for p in user.prompts] == \
        [f"{BROKER_KC}/v1/authorize/mockhub-jwt"]
    assert "Linear" in user.prompts[0].message
    quiet = User("alice", action="decline")
    assert not (await _call("alice", "github_get_me", user=quiet)).is_error
    assert quiet.prompts == []


async def test_each_upstream_gets_its_own_token_and_policy():
    await _call("alice", "github_get_me", user=User("alice", action="decline"))
    await _call("alice", "linear_get_issue", {"id": "LIN-2"},
                user=User("alice", action="decline"))
    github, linear = _last_call("github"), _last_call("linear")
    assert linear["tool"] == "get_issue" and linear["args"] == {"id": "LIN-2"}
    assert linear["scheme"] == "Bearer"
    assert (linear["readonly"], linear["lockdown"]) == (None, None)   # Linear: read-only URL
    assert github["readonly"] == "true"
    assert github["token_fp"] != linear["token_fp"]                  # different vendors


# ------------------------------------------ servers with their own sign-in

async def test_atlassian_tools_are_listed_once_connected():
    user = User("alice")
    async with gateway_client("alice", user) as c:
        r = await c.call_tool("connect_atlassian", {}, raise_on_error=False)
        names = {t.name for t in await c.list_tools()}
    assert not r.is_error, r.content[0].text
    assert "Atlassian is connected; 8 Atlassian tools" in r.content[0].text
    assert [p.url.split("?")[0] for p in user.prompts] == \
        [f"{BROKER_KC}/v1/authorize/mockhub-atlassian"]
    assert ATLASSIAN <= names and "atlassian_executeWrite" not in names


async def test_atlassian_calls_keep_working_across_refreshes():
    """Every call refreshes (60s mock tokens); a refresh that didn't repeat
    the resource would get invalid_target, and a token not issued for the
    Atlassian stand-in would get 401."""
    before = mock_state()["counters"]
    for key in ("LAB-1", "LAB-2"):
        r = await _call("alice", "atlassian_getJiraIssue",
                        {"cloudId": "c1", "issueIdOrKey": key},
                        user=User("alice", action="decline"))
        assert not r.is_error, r.content[0].text
        assert _last_call("atlassian")["args"]["issueIdOrKey"] == key
    after = mock_state()["counters"]
    assert after["invalid_target"] == before["invalid_target"]
    assert after["token_refresh"] >= before["token_refresh"] + 2


async def test_tokens_only_work_at_the_server_they_were_issued_for():
    await _call("alice", "connect_cloudflare", user=User("alice"))
    hub = hub_jwt("alice")
    tokens = {v: resolve_kc(hub, v).json()["access_token"]
              for v in ("mockhub", *OWN_SIGN_IN)}
    for path, vendor in [("/atlassian/mcp", "mockhub-atlassian"),
                         ("/cloudflare/mcp", "mockhub-cloudflare")]:
        for holder, token in tokens.items():
            r = httpx.post(f"{MOCK_MCP}{path}", json=INIT, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"})
            assert (r.status_code == 200) == (holder == vendor), (path, holder, r.status_code)


# ---------------------------------------------------------- consent outcomes

async def test_declining_leaves_github_disconnected():
    revoke_kc(hub_jwt("alice"))
    user = User("alice", action="decline")
    r = await _call("alice", "github_get_me", user=user)
    assert r.is_error and "not connected" in r.content[0].text
    assert len(user.prompts) == 1
    assert resolve_kc(hub_jwt("alice")).status_code == 404


async def test_link_opened_by_someone_else_never_connects():
    """Alice's prompt, but Bob signs in: the broker refuses (403) and the
    gateway gives up after its bounded wait."""
    revoke_kc(hub_jwt("alice"))
    user = User("alice", sign_in_as="bob")
    r = await _call("alice", "github_get_me", user=user)
    assert user.pages == [403]
    assert r.is_error and "not connected yet" in r.content[0].text
    assert resolve_kc(hub_jwt("alice")).status_code == 404


async def test_client_without_url_elicitation_is_given_the_link():
    revoke_kc(hub_jwt("alice"))
    r = await _call("alice", "github_get_me")               # no elicitation handler
    assert r.is_error and f"{BROKER_KC}/v1/authorize/mockhub?txn=" in r.content[0].text


async def test_revoked_connection_prompts_again():
    user = User("alice")
    r = await _call("alice", "github_get_me", user=user)   # reconnects (after the tests above)
    assert not r.is_error and len(user.prompts) == 1
    revoke_kc(hub_jwt("alice"))
    again = User("alice")
    r = await _call("alice", "github_get_me", user=again)
    assert not r.is_error and len(again.prompts) == 1


# ----------------------------------------------------------------- failures

@contextlib.contextmanager
def paused(container: str):
    subprocess.run(["docker", "pause", container], check=True, capture_output=True)
    try:
        yield
    finally:
        subprocess.run(["docker", "unpause", container], capture_output=True)


async def test_broker_outage_is_retryable_and_never_asks_to_connect():
    user = User("alice", action="decline")
    token = mcp_token("alice")
    with paused("vtb-broker-kc"):
        r = await _call("alice", "github_get_me", user=user, token=token)
    assert r.is_error and "retry shortly" in r.content[0].text
    assert user.prompts == []


async def test_github_outage_is_retryable():
    token = mcp_token("alice")
    with paused("vtb-mock-mcp"):
        r = await _call("alice", "github_get_me", user=User("alice", action="decline"),
                        token=token)
    assert r.is_error and "unavailable" in r.content[0].text


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-11-25", "capabilities": {},
    "clientInfo": {"name": "t", "version": "0"}}}


@pytest.mark.parametrize("path", ["/github/mcp", "/linear/mcp", "/atlassian/mcp",
                                  "/cloudflare/mcp"])
@pytest.mark.parametrize("auth", [None, "Basic dXNlcjpwYXNz", "Bearer not-a-live-token"])
def test_stand_ins_refuse_calls_without_a_live_vendor_token(path, auth):
    """The stand-ins behave like the real servers: no live token, 401."""
    headers = {"Content-Type": "application/json", **({"Authorization": auth} if auth else {})}
    assert httpx.post(f"{MOCK_MCP}{path}", json=INIT, headers=headers).status_code == 401


@pytest.mark.parametrize("which", ["hub-jwt", "no-gateway-scope", "garbage"])
def test_tokens_not_issued_for_the_gateway_are_rejected(which):
    token = {"hub-jwt": lambda: hub_jwt("alice"),
             "no-gateway-scope": lambda: mcp_token("alice", scope="openid"),
             "garbage": lambda: "not-a-token"}[which]()
    r = httpx.post(GATEWAY_MCP, json=INIT, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]


def test_no_token_material_in_any_container_log():
    mcp = mcp_token("alice")
    with ThreadPoolExecutor(1) as pool:                     # a real call with this token
        r = pool.submit(asyncio.run, _call("alice", "github_get_me", token=mcp)).result()
        r2 = pool.submit(asyncio.run, _call("alice", "linear_list_issues", token=mcp,
                                            user=User("alice"))).result()
    assert not r.is_error and not r2.is_error
    hub = hub_jwt("alice")
    github = resolve_kc(hub).json()["access_token"]
    linear = resolve_kc(hub, "mockhub-jwt").json()["access_token"]
    for secret in (mcp, hub, github, linear):
        assert grep_container_logs(secret, since="10m") == {}
