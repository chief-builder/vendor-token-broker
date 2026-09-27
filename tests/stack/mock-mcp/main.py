"""Stand-ins for vendor MCP servers, one per path: /github/mcp mirrors
GitHub's MCP tools, /linear/mcp mirrors Linear's. Every request must carry a
live mockhub access token (checked against mock-vendor's /user), so an
expired, refreshed-away or revoked token gets HTTP 401 as it would from the
real server. Each personality also offers one write tool the gateway's
allowlist must hide. /_test/state reports what each call carried (upstream,
Authorization scheme, policy headers, token fingerprint), never tokens."""
import hashlib
import os
from contextlib import asynccontextmanager

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

VENDOR = os.environ.get("MOCK_VENDOR_URL", "http://mock-vendor:8310")
state: dict = {"calls": [], "unauthorized": 0}


class RequireVendorToken:
    """401 unless the bearer is a live mockhub access token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].endswith("/mcp"):
            return await self.app(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        token = auth.split(None, 1)[-1] if " " in auth else ""
        ok = 0
        if token:
            async with httpx.AsyncClient(timeout=5) as http:
                ok = (await http.get(f"{VENDOR}/user",
                                     headers={"Authorization": f"Bearer {token}"})).status_code
        if ok != 200:
            state["unauthorized"] += 1
            return await JSONResponse({"error": "unauthorized"}, status_code=401,
                                      headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        return await self.app(scope, receive, send)


def _record(upstream: str, tool: str, **args) -> None:
    h = get_http_headers(include={"authorization"})
    scheme, _, token = h.get("authorization", "").partition(" ")
    state["calls"].append({
        "upstream": upstream, "tool": tool, "args": args, "scheme": scheme,
        "token_fp": hashlib.sha256(token.encode()).hexdigest()[:16],
        "readonly": h.get("x-mcp-readonly"), "lockdown": h.get("x-mcp-lockdown"),
        "toolsets": h.get("x-mcp-toolsets")})


# ------------------------------------------------------------------ GitHub
github = FastMCP("mock-github-mcp")
ISSUES = [{"number": 1, "title": "First issue", "state": "open"},
          {"number": 2, "title": "Second issue", "state": "closed"}]


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
LINEAR_ISSUES = [{"id": "LIN-1", "title": "Fix login", "status": "In Progress"},
                 {"id": "LIN-2", "title": "Write docs", "status": "Todo"}]


def _linear_read(name: str, doc: str, result):
    def tool(query: str = "") -> object:
        _record("linear", name, query=query)
        return result
    tool.__name__, tool.__doc__ = name, doc
    linear.tool(tool)


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
    _linear_read(_name, _doc, _result)


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


# ------------------------------------------------------------------- app
github_app, linear_app = github.http_app(path="/mcp"), linear.http_app(path="/mcp")


@asynccontextmanager
async def lifespan(app):
    async with github_app.lifespan(app), linear_app.lifespan(app):
        yield


async def test_state(request):
    return JSONResponse(state)


async def test_reset(request):
    state["calls"].clear()
    state["unauthorized"] = 0
    return JSONResponse({"ok": True})


app = RequireVendorToken(Starlette(lifespan=lifespan, routes=[
    Route("/_test/state", test_state), Route("/_test/reset", test_reset, methods=["POST"]),
    Mount("/github", github_app), Mount("/linear", linear_app)]))
