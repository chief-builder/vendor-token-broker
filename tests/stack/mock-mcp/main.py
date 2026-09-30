"""Stand-ins for vendor MCP servers, one per path: /github/mcp mirrors
GitHub's MCP tools, /linear/mcp Linear's, /atlassian/mcp Atlassian's and
/cloudflare/mcp Cloudflare's. Every request must carry a live mockhub access
token (checked against mock-vendor's /introspect), so an expired,
refreshed-away or revoked token gets HTTP 401 as it would from the real
server. Atlassian and Cloudflare run their own sign-in, so like the real
servers they also refuse tokens not issued for them (RFC 8707 resource).
GitHub, Linear and Atlassian also offer a write tool the gateway's
allowlist must hide. /_test/state reports what each call carried (upstream,
Authorization scheme, policy headers, token fingerprint), never tokens."""

import hashlib
import os
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

VENDOR = os.environ.get("MOCK_VENDOR_URL", "http://mock-vendor:8310")
PUBLIC_BASE = os.environ.get("MOCK_MCP_BASE", "http://mock-mcp:8330")
BOUND = ("/atlassian/mcp", "/cloudflare/mcp")  # tokens must be issued for these
state: dict = {"calls": [], "unauthorized": 0}


class RequireVendorToken:
    """401 unless the bearer is a live mockhub access token (issued for this
    server, on the paths that run their own sign-in)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].endswith("/mcp"):
            return await self.app(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        token = auth.split(None, 1)[-1] if " " in auth else ""
        found = {"active": False}
        if token:
            async with httpx.AsyncClient(timeout=5) as http:
                found = (await http.post(f"{VENDOR}/introspect", data={"token": token})).json()
        ok = found["active"] and (
            scope["path"] not in BOUND or found.get("aud") == PUBLIC_BASE + scope["path"]
        )
        if not ok:
            state["unauthorized"] += 1
            return await JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )(scope, receive, send)
        return await self.app(scope, receive, send)


def _record(upstream: str, tool: str, **args) -> None:
    h = get_http_headers(include={"authorization"})
    scheme, _, token = h.get("authorization", "").partition(" ")
    state["calls"].append(
        {
            "upstream": upstream,
            "tool": tool,
            "args": args,
            "scheme": scheme,
            "token_fp": hashlib.sha256(token.encode()).hexdigest()[:16],
            "readonly": h.get("x-mcp-readonly"),
            "lockdown": h.get("x-mcp-lockdown"),
            "toolsets": h.get("x-mcp-toolsets"),
        }
    )


def _read_tool(server: FastMCP, upstream: str, name: str, doc: str, result):
    def tool(query: str = "") -> object:
        _record(upstream, name, query=query)
        return result

    tool.__name__, tool.__doc__ = name, doc
    server.tool(tool)


# ------------------------------------------------------------------ GitHub
github = FastMCP("mock-github-mcp")
ISSUES = [
    {"number": 1, "title": "First issue", "state": "open"},
    {"number": 2, "title": "Second issue", "state": "closed"},
]


@github.tool
def get_me() -> dict:
    """Details of the authenticated GitHub user."""
    _record("github", "get_me")
    return {"login": "octocat-lab", "id": 4217, "type": "User"}


@github.tool
def search_repositories(query: str) -> dict:
    """Search repositories."""
    _record("github", "search_repositories", query=query)
    return {"total_count": 1, "items": [{"full_name": "octocat-lab/hello-world"}]}


@github.tool
def get_file_contents(owner: str, repo: str, path: str = "") -> str:
    """Contents of a file or directory."""
    _record("github", "get_file_contents", owner=owner, repo=repo, path=path)
    return f"# {repo}\\nHello from {owner}/{repo}/{path}\\n"


@github.tool
def list_issues(owner: str, repo: str) -> list[dict]:
    """List issues in a repository."""
    _record("github", "list_issues", owner=owner, repo=repo)
    return ISSUES


@github.tool
def issue_read(owner: str, repo: str, issue_number: int) -> dict:
    """Read one issue."""
    _record("github", "issue_read", owner=owner, repo=repo, issue_number=issue_number)
    return next((i for i in ISSUES if i["number"] == issue_number), {"error": "not found"})


@github.tool
def list_pull_requests(owner: str, repo: str) -> list[dict]:
    """List pull requests."""
    _record("github", "list_pull_requests", owner=owner, repo=repo)
    return [{"number": 7, "title": "A pull request"}]


@github.tool
def pull_request_read(owner: str, repo: str, pullNumber: int) -> dict:  # noqa: N803
    """Read one pull request."""
    _record("github", "pull_request_read", owner=owner, repo=repo, pullNumber=pullNumber)
    return {"number": pullNumber, "title": "A pull request"}


@github.tool
def create_issue(owner: str, repo: str, title: str) -> dict:
    """Create an issue (a write tool: the gateway's allowlist hides it)."""
    _record("github", "create_issue", owner=owner, repo=repo, title=title)
    return {"number": 3, "title": title}


# ------------------------------------------------------------------ Linear
linear = FastMCP("mock-linear-mcp")
LINEAR_ISSUES = [
    {"id": "LIN-1", "title": "Fix login", "status": "In Progress"},
    {"id": "LIN-2", "title": "Write docs", "status": "Todo"},
]


for _name, _doc, _result in [
    ("list_issues", "List issues.", {"issues": LINEAR_ISSUES}),
    ("list_comments", "List comments on an issue.", {"comments": []}),
    ("list_projects", "List projects.", {"projects": [{"name": "Gateway"}]}),
    ("get_project", "Get a project.", {"name": "Gateway"}),
    ("list_cycles", "List cycles.", {"cycles": []}),
    ("list_teams", "List teams.", {"teams": [{"name": "Platform"}]}),
    ("get_team", "Get a team.", {"name": "Platform"}),
    ("list_issue_statuses", "List issue statuses.", {"statuses": ["Todo", "In Progress"]}),
    ("list_users", "List users.", {"users": [{"name": "Lin Lab"}]}),
    ("get_user", "Get a user.", {"name": "Lin Lab"}),
    ("list_documents", "List documents.", {"documents": []}),
    ("get_document", "Get a document.", {"title": "Runbook"}),
]:
    _read_tool(linear, "linear", _name, _doc, _result)


@linear.tool
def get_issue(id: str) -> dict:  # noqa: A002 - Linear's parameter name
    """Get one issue."""
    _record("linear", "get_issue", id=id)
    return next((i for i in LINEAR_ISSUES if i["id"] == id), {"error": "not found"})


@linear.tool
def save_issue(title: str) -> dict:
    """Create or update an issue (a write tool: the gateway's allowlist hides it)."""
    _record("linear", "save_issue", title=title)
    return {"id": "LIN-3", "title": title}


# --------------------------------------------------------------- Atlassian
atlassian = FastMCP("mock-atlassian-mcp")
CLOUD_ID = "00000000-0000-4000-8000-00000000a71a"


for _name, _doc, _result in [
    ("atlassianUserInfo", "The signed-in Atlassian user.", {"account_id": "at-4217"}),
    (
        "getAccessibleAtlassianResources",
        "Sites this user can reach.",
        [{"id": CLOUD_ID, "name": "lab-site"}],
    ),
    (
        "searchJiraIssuesUsingJql",
        "Search Jira issues with JQL.",
        {"issues": [{"key": "LAB-1", "summary": "Gateway rollout"}]},
    ),
    ("getConfluenceContent", "Read a Confluence page.", {"title": "Runbook"}),
    ("searchConfluence", "Search Confluence.", {"results": [{"title": "Runbook"}]}),
    ("executeRead", "Run a read-only Atlassian API call.", {"ok": True}),
    ("discover", "Describe what this server can do.", {"products": ["jira", "confluence"]}),
]:
    _read_tool(atlassian, "atlassian", _name, _doc, _result)


@atlassian.tool
def getJiraIssue(cloudId: str, issueIdOrKey: str) -> dict:  # noqa: N802, N803
    """Read one Jira issue."""
    _record("atlassian", "getJiraIssue", cloudId=cloudId, issueIdOrKey=issueIdOrKey)
    return {"key": issueIdOrKey, "summary": "Gateway rollout", "status": "In Progress"}


@atlassian.tool
def executeWrite(request: str) -> dict:  # noqa: N802
    """Run a writing Atlassian API call (the gateway's allowlist hides it)."""
    _record("atlassian", "executeWrite", request=request)
    return {"ok": True}


# -------------------------------------------------------------- Cloudflare
cloudflare = FastMCP("mock-cloudflare-mcp")
for _name, _doc, _result in [
    ("search", "Search the Cloudflare API spec.", {"endpoints": ["GET /accounts"]}),
    ("docs", "Search Cloudflare's documentation.", {"pages": ["Workers"]}),
    (
        "execute",
        "Call the Cloudflare API (read-only via the granted scopes).",
        {"result": [{"name": "chantalong"}]},
    ),
]:
    _read_tool(cloudflare, "cloudflare", _name, _doc, _result)


# ------------------------------------------------------------------- app
servers = {"github": github, "linear": linear, "atlassian": atlassian, "cloudflare": cloudflare}
apps = {name: server.http_app(path="/mcp") for name, server in servers.items()}


@asynccontextmanager
async def lifespan(app):
    async with AsyncExitStack() as stack:
        for sub in apps.values():
            await stack.enter_async_context(sub.lifespan(app))
        yield


async def test_state(request):
    return JSONResponse(state)


async def test_reset(request):
    state["calls"].clear()
    state["unauthorized"] = 0
    return JSONResponse({"ok": True})


app = RequireVendorToken(
    Starlette(
        lifespan=lifespan,
        routes=[
            Route("/_test/state", test_state),
            Route("/_test/reset", test_reset, methods=["POST"]),
            *[Mount(f"/{name}", sub) for name, sub in apps.items()],
        ],
    )
)
