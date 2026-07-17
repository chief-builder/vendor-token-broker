"""mockhub — a GitHub-class vendor AS + MCP endpoint for the phase 5 gates.

Deliberately hostile in the ways that matter to the broker design:
- 60 second access tokens (plan §3: 'set a 60s access-token TTL') so every
  resolve lands inside the refresh buffer;
- rotating refresh tokens where replaying a consumed token REVOKES the whole
  family (GitHub-class behavior, design §9);
- PKCE S256 verified for real, so a broker that loses the verifier fails;
- RFC 8414 metadata and RFC 9207 iss on the redirect, so the broker's
  discovery and mix-up defense run against a real implementation;
- RFC 7009 revocation endpoint.

/_test/* endpoints expose call counters and a revoke-family switch for the
acceptance suite. Nothing here persists — restart resets the vendor.
"""
import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

# Browser-facing endpoints advertise localhost (host side of the port map);
# server-to-server endpoints advertise the compose-network hostname.
PUBLIC_URL = os.environ.get("MOCK_PUBLIC_URL", "http://localhost:8310")
INTERNAL_URL = os.environ.get("MOCK_INTERNAL_URL", "http://mock-vendor:8310")
ISSUER = INTERNAL_URL
CLIENT_ID = os.environ.get("MOCK_CLIENT_ID", "mcp-lab-broker")
CLIENT_SECRET = os.environ.get("MOCK_CLIENT_SECRET", "mock-secret")
AT_TTL = int(os.environ.get("MOCK_AT_TTL", "60"))
MOCK_USER = {"id": "mock-4217", "login": "octocat-lab"}

app = FastAPI(title="mockhub")

codes: dict[str, dict] = {}
families: dict[str, dict] = {}         # family_id -> {gen, active_rt, revoked}
refresh_tokens: dict[str, dict] = {}   # rt -> {family, gen, consumed}
access_tokens: dict[str, dict] = {}    # at -> {family, exp, scopes}
counters = {"authorize": 0, "token_code": 0, "token_refresh": 0,
            "rt_replay": 0, "revoke": 0, "mcp_calls": 0, "mcp_unauthorized": 0}
issues: list[dict] = []


def _client_ok(client_id: str | None, client_secret: str | None) -> bool:
    return client_id == CLIENT_ID and client_secret == CLIENT_SECRET


def _mint(family_id: str, scopes: str) -> dict:
    fam = families[family_id]
    fam["gen"] += 1
    at, rt = f"mock-at-{secrets.token_urlsafe(24)}", f"mock-rt-{secrets.token_urlsafe(24)}"
    if fam.get("active_rt"):
        refresh_tokens[fam["active_rt"]]["consumed"] = True
    fam["active_rt"] = rt
    refresh_tokens[rt] = {"family": family_id, "gen": fam["gen"], "consumed": False}
    access_tokens[at] = {"family": family_id, "exp": time.time() + AT_TTL,
                         "scopes": scopes}
    return {"access_token": at, "token_type": "bearer", "expires_in": AT_TTL,
            "refresh_token": rt, "scope": scopes}


@app.get("/.well-known/oauth-authorization-server")
async def metadata():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{PUBLIC_URL}/authorize",
        "token_endpoint": f"{INTERNAL_URL}/token",
        "revocation_endpoint": f"{INTERNAL_URL}/revoke",
        "userinfo_endpoint": f"{INTERNAL_URL}/user",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True,
    }


@app.get("/authorize")
async def authorize(client_id: str, redirect_uri: str, state: str,
                    code_challenge: str, response_type: str = "code",
                    code_challenge_method: str = "S256", scope: str = ""):
    counters["authorize"] += 1
    if client_id != CLIENT_ID or response_type != "code" or code_challenge_method != "S256":
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    code = f"mock-code-{secrets.token_urlsafe(16)}"
    codes[code] = {"challenge": code_challenge, "redirect_uri": redirect_uri,
                   "scope": scope, "created_at": time.time(), "used": False}
    # Auto-consent as the fixed mock user; RFC 9207 iss on the response.
    return RedirectResponse(
        f"{redirect_uri}?{urlencode({'code': code, 'state': state, 'iss': ISSUER})}")


@app.post("/token")
async def token(request: Request):
    form = dict((await request.form()).items())
    if not _client_ok(form.get("client_id"), form.get("client_secret")):
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if form.get("grant_type") == "authorization_code":
        counters["token_code"] += 1
        rec = codes.get(form.get("code", ""))
        if (rec is None or rec["used"] or time.time() - rec["created_at"] > 300
                or rec["redirect_uri"] != form.get("redirect_uri")):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != rec["challenge"]:
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "pkce"}, status_code=400)
        rec["used"] = True
        family_id = f"fam-{secrets.token_urlsafe(8)}"
        families[family_id] = {"gen": 0, "active_rt": None, "revoked": False}
        return _mint(family_id, rec["scope"])

    if form.get("grant_type") == "refresh_token":
        counters["token_refresh"] += 1
        rt = refresh_tokens.get(form.get("refresh_token", ""))
        if rt is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        fam = families[rt["family"]]
        if fam["revoked"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if rt["consumed"]:
            # Replay of a rotated-away refresh token burns the family (§9).
            counters["rt_replay"] += 1
            fam["revoked"] = True
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "replay"}, status_code=400)
        return _mint(rt["family"], "issues:read issues:write")

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


@app.post("/revoke")
async def revoke(token: str = Form(...), token_type_hint: str = Form(""),
                 client_id: str = Form(""), client_secret: str = Form("")):
    """RFC 7009: always 200, family revoked if the token is known."""
    counters["revoke"] += 1
    if not _client_ok(client_id, client_secret):
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    fam_id = (refresh_tokens.get(token, {}) or access_tokens.get(token, {})).get("family")
    if fam_id:
        families[fam_id]["revoked"] = True
    return JSONResponse({})


def _bearer(request: Request) -> dict | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    at = access_tokens.get(auth.split(None, 1)[1])
    if at is None or at["exp"] < time.time() or families[at["family"]]["revoked"]:
        return None
    return at


@app.get("/user")
async def user(request: Request):
    if _bearer(request) is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return MOCK_USER


@app.post("/mcp")
async def mcp(request: Request):
    """Fake vendor MCP endpoint: create_issue / list_issues over JSON-RPC."""
    if _bearer(request) is None:
        counters["mcp_unauthorized"] += 1
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})
    counters["mcp_calls"] += 1
    body = await request.json()
    name = (body.get("params") or {}).get("name")
    args = (body.get("params") or {}).get("arguments") or {}
    if body.get("method") != "tools/call" or name not in ("create_issue", "list_issues"):
        result = {"error": "unknown tool"}
    elif name == "create_issue":
        issues.append({"number": len(issues) + 1, **args})
        result = {"ok": True, "issue_number": len(issues)}
    else:
        result = {"issues": issues}
    return {"jsonrpc": "2.0", "id": body.get("id"),
            "result": {"content": [{"type": "text", "text": json.dumps(result)}]}}


@app.get("/_test/state")
async def test_state():
    return {"counters": counters, "families": families, "issue_count": len(issues)}


@app.post("/_test/revoke_family")
async def test_revoke_family():
    """Simulate 'user revoked at vendor' — next refresh gets invalid_grant."""
    for fam in families.values():
        fam["revoked"] = True
    return {"revoked_families": len(families)}


@app.post("/_test/reset")
async def test_reset():
    for store in (codes, families, refresh_tokens, access_tokens):
        store.clear()
    issues.clear()
    for k in counters:
        counters[k] = 0
    return {"ok": True}
