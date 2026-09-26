"""Stand-in for GitHub's remote MCP server. Every request must carry a live
mockhub access token (checked against mock-vendor's /user), so an expired,
refreshed-away or revoked token gets HTTP 401 as it would from GitHub. The
tools mirror GitHub's names, including one write tool the gateway's
allowlist must hide. /_test/state reports what each call carried (token
fingerprints, never tokens)."""
import hashlib
import os

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

VENDOR = os.environ.get("MOCK_VENDOR_URL", "http://mock-vendor:8310")
state: dict = {"calls": [], "unauthorized": 0}
mcp = FastMCP("mock-github-mcp")


class RequireVendorToken:
    """401 unless the bearer is a live mockhub access token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
            return await self.app(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        async with httpx.AsyncClient(timeout=5) as http:
            ok = (await http.get(f"{VENDOR}/user", headers={"Authorization": auth})).status_code
        if ok != 200:
            state["unauthorized"] += 1
            return await JSONResponse({"error": "unauthorized"}, status_code=401,
                                      headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        return await self.app(scope, receive, send)


def _record(tool: str, **args) -> None:
    h = get_http_headers(include={"authorization"})
    token = h.get("authorization", "").split(None, 1)[-1]
    state["calls"].append({
        "tool": tool, "args": args,
        "token_fp": hashlib.sha256(token.encode()).hexdigest()[:16],
        "readonly": h.get("x-mcp-readonly"), "lockdown": h.get("x-mcp-lockdown"),
        "toolsets": h.get("x-mcp-toolsets")})


ISSUES = [{"number": 1, "title": "First issue", "state": "open"},
          {"number": 2, "title": "Second issue", "state": "closed"}]


@mcp.tool
def get_me() -> dict:
    """Details of the authenticated GitHub user."""
    _record("get_me")
    return {"login": "octocat-lab", "id": 4217, "type": "User"}


@mcp.tool
def search_repositories(query: str) -> dict:
    """Search repositories."""
    _record("search_repositories", query=query)
    return {"total_count": 1, "items": [{"full_name": "octocat-lab/hello-world"}]}


@mcp.tool
def get_file_contents(owner: str, repo: str, path: str = "") -> str:
    """Contents of a file or directory."""
    _record("get_file_contents", owner=owner, repo=repo, path=path)
    return f"# {repo}\nHello from {owner}/{repo}/{path}\n"


@mcp.tool
def list_issues(owner: str, repo: str) -> list[dict]:
    """List issues in a repository."""
    _record("list_issues", owner=owner, repo=repo)
    return ISSUES


@mcp.tool
def issue_read(owner: str, repo: str, issue_number: int) -> dict:
    """Read one issue."""
    _record("issue_read", owner=owner, repo=repo, issue_number=issue_number)
    return next((i for i in ISSUES if i["number"] == issue_number), {"error": "not found"})


@mcp.tool
def list_pull_requests(owner: str, repo: str) -> list[dict]:
    """List pull requests."""
    _record("list_pull_requests", owner=owner, repo=repo)
    return [{"number": 7, "title": "A pull request"}]


@mcp.tool
def pull_request_read(owner: str, repo: str, pull_number: int) -> dict:
    """Read one pull request."""
    _record("pull_request_read", owner=owner, repo=repo, pull_number=pull_number)
    return {"number": pull_number, "title": "A pull request"}


@mcp.tool
def create_issue(owner: str, repo: str, title: str) -> dict:
    """Create an issue (a write tool: the gateway's read-only allowlist hides it)."""
    _record("create_issue", owner=owner, repo=repo, title=title)
    return {"number": 3, "title": title}


async def test_state(request):
    return JSONResponse(state)


async def test_reset(request):
    state["calls"].clear()
    state["unauthorized"] = 0
    return JSONResponse({"ok": True})


app = mcp.http_app(path="/mcp", middleware=[Middleware(RequireVendorToken)])
app.router.routes += [Route("/_test/state", test_state), Route("/_test/reset", test_reset,
                                                                methods=["POST"])]
