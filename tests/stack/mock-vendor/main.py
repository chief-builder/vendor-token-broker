"""mockhub — a GitHub-class vendor AS + MCP endpoint for the phase 5 gates.

Deliberately hostile in the ways that matter to the broker design:
- 60 second access tokens (plan §3: 'set a 60s access-token TTL') so every
  resolve lands inside the refresh buffer;
- rotating refresh tokens where replaying a consumed token REVOKES the whole
  family (GitHub-class behavior, design §8);
- PKCE S256 verified for real, so a broker that loses the verifier fails;
- RFC 8414 metadata and RFC 9207 iss on the redirect, so the broker's
  discovery and mix-up defense run against a real implementation;
- RFC 7009 revocation endpoint;
- an MCP-authorization-server variant (metadata at .../mcp) with RFC 7591
  dynamic registration. Codes, refresh-token families and access tokens are
  bound to the RFC 8707 `resource` sent on authorize; a code exchange or
  refresh that doesn't repeat it gets invalid_target, and /introspect tells
  the MCP stand-ins which server a token was issued for.

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

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

# Browser-facing endpoints advertise localhost (host side of the port map);
# server-to-server endpoints advertise the compose-network hostname.
PUBLIC_URL = os.environ.get("MOCK_PUBLIC_URL", "http://localhost:8310")
INTERNAL_URL = os.environ.get("MOCK_INTERNAL_URL", "http://mock-vendor:8310")
ISSUER = INTERNAL_URL
CLIENT_ID = os.environ.get("MOCK_CLIENT_ID", "mcp-lab-broker")
CLIENT_SECRET = os.environ.get("MOCK_CLIENT_SECRET", "mock-secret")
# private_key_jwt client (RFC 7523): assertions are verified for real against
# this public key; the broker holds the matching private key in custody.
JWT_CLIENT_ID = os.environ.get("MOCK_JWT_CLIENT_ID", "mcp-lab-broker-jwt")
JWT_PUBLIC_KEY = None
if os.environ.get("MOCK_JWT_PUBLIC_KEY_FILE"):
    with open(os.environ["MOCK_JWT_PUBLIC_KEY_FILE"]) as fh:
        JWT_PUBLIC_KEY = fh.read()
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
AT_TTL = int(os.environ.get("MOCK_AT_TTL", "60"))
MOCK_USER = {"id": "mock-4217", "login": "octocat-lab"}

app = FastAPI(title="mockhub")

codes: dict[str, dict] = {}
families: dict[str, dict] = {}         # family_id -> {gen, active_rt, revoked}
refresh_tokens: dict[str, dict] = {}   # rt -> {family, gen, consumed}
access_tokens: dict[str, dict] = {}    # at -> {family, exp, scopes}
counters = {"authorize": 0, "token_code": 0, "token_refresh": 0,
            "rt_replay": 0, "revoke": 0, "mcp_calls": 0, "mcp_unauthorized": 0,
            "client_assertions": 0, "bad_assertions": 0, "invalid_target": 0}
issues: list[dict] = []
registered: dict[str, dict] = {}       # client_id -> RFC 7591 registration (kept on reset)


def _client_ok(client_id: str | None, client_secret: str | None) -> bool:
    if client_id in registered:
        return client_secret == registered[client_id]["client_secret"]
    return client_id == CLIENT_ID and client_secret == CLIENT_SECRET


def _assertion_ok(form: dict) -> bool:
    """RFC 7523 §3: verified signature, iss == sub == a registered client,
    aud identifies this AS, exp/jti present."""
    counters["client_assertions"] += 1
    if JWT_PUBLIC_KEY is None:
        return False
    try:
        claims = jwt.decode(
            form.get("client_assertion", ""), JWT_PUBLIC_KEY,
            algorithms=["RS256"],
            audience=[f"{INTERNAL_URL}/token", ISSUER],
            options={"require": ["exp", "iss", "sub", "aud", "jti"]})
    except Exception:
        counters["bad_assertions"] += 1
        return False
    if not (claims["iss"] == claims["sub"] == JWT_CLIENT_ID):
        counters["bad_assertions"] += 1
        return False
    if form.get("client_id") and form["client_id"] != JWT_CLIENT_ID:
        counters["bad_assertions"] += 1
        return False
    return True


def _authenticated(request: Request, form: dict) -> bool:
    """Accept client_secret_post, client_secret_basic, or private_key_jwt."""
    if form.get("client_assertion_type") == ASSERTION_TYPE:
        return _assertion_ok(form)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(auth.split(None, 1)[1]).decode().partition(":")
        except Exception:
            return False
        return _client_ok(user, pw)
    return _client_ok(form.get("client_id"), form.get("client_secret"))


def _mint(family_id: str, scopes: str) -> dict:
    fam = families[family_id]
    fam["gen"] += 1
    at, rt = f"mock-at-{secrets.token_urlsafe(24)}", f"mock-rt-{secrets.token_urlsafe(24)}"
    if fam.get("active_rt"):
        refresh_tokens[fam["active_rt"]]["consumed"] = True
    fam["active_rt"] = rt
    refresh_tokens[rt] = {"family": family_id, "gen": fam["gen"], "consumed": False}
    access_tokens[at] = {"family": family_id, "exp": time.time() + AT_TTL,
                         "scopes": scopes, "resource": fam["resource"]}
    return {"access_token": at, "token_type": "bearer", "expires_in": AT_TTL,
            "refresh_token": rt, "scope": scopes}


@app.get("/.well-known/oauth-authorization-server")
async def metadata():
    return _metadata()


@app.get("/.well-known/oauth-authorization-server/norevoke")
async def metadata_without_revocation():
    """RFC 8414 path-suffix variant for a vendor that offers no revocation
    endpoint (registry entry mockhub-norevoke)."""
    meta = _metadata()
    del meta["revocation_endpoint"]
    return meta


@app.get("/.well-known/oauth-authorization-server/mcp")
async def metadata_mcp():
    """RFC 8414 path-suffix variant playing an MCP server's own authorization
    server (Atlassian, Cloudflare): offers dynamic registration. The
    registration endpoint is host-facing, where an admin runs the script."""
    return {**_metadata(), "registration_endpoint": f"{PUBLIC_URL}/register"}


@app.post("/register")
async def register(request: Request):
    """RFC 7591: a confidential client_secret_post client."""
    body = await request.json()
    if not body.get("redirect_uris"):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    reg = {**body, "client_id": f"mock-dcr-{secrets.token_urlsafe(8)}",
           "client_secret": secrets.token_urlsafe(24), "client_id_issued_at": int(time.time()),
           "client_secret_expires_at": 0, "token_endpoint_auth_method": "client_secret_post"}
    registered[reg["client_id"]] = reg
    return JSONResponse(reg, status_code=201)


def _metadata() -> dict:
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
                    code_challenge_method: str = "S256", scope: str = "",
                    resource: str | None = None):
    counters["authorize"] += 1
    if client_id not in (CLIENT_ID, JWT_CLIENT_ID, *registered) or response_type != "code" \
            or code_challenge_method != "S256":
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    code = f"mock-code-{secrets.token_urlsafe(16)}"
    codes[code] = {"challenge": code_challenge, "redirect_uri": redirect_uri,
                   "scope": scope, "resource": resource, "created_at": time.time(),
                   "used": False}
    # Auto-consent as the fixed mock user; RFC 9207 iss on the response.
    return RedirectResponse(
        f"{redirect_uri}?{urlencode({'code': code, 'state': state, 'iss': ISSUER})}")


@app.post("/token")
async def token(request: Request):
    form = dict((await request.form()).items())
    if not _authenticated(request, form):
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
        if form.get("resource") != rec["resource"]:
            counters["invalid_target"] += 1
            return JSONResponse({"error": "invalid_target"}, status_code=400)
        rec["used"] = True
        family_id = f"fam-{secrets.token_urlsafe(8)}"
        families[family_id] = {"gen": 0, "active_rt": None, "revoked": False,
                               "resource": rec["resource"]}
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
            # Replay of a rotated-away refresh token burns the family (§8).
            counters["rt_replay"] += 1
            fam["revoked"] = True
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "replay"}, status_code=400)
        if form.get("resource") != fam["resource"]:
            counters["invalid_target"] += 1
            return JSONResponse({"error": "invalid_target"}, status_code=400)
        return _mint(rt["family"], "issues:read issues:write")

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


@app.post("/revoke")
async def revoke(request: Request):
    """RFC 7009: always 200, family revoked if the token is known."""
    counters["revoke"] += 1
    form = dict((await request.form()).items())
    if not _authenticated(request, form):
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    token = form.get("token", "")
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


@app.post("/introspect")
async def introspect(request: Request):
    """RFC 7662-style, for the MCP stand-ins: is the token live, and which
    server (`aud`, the RFC 8707 resource) was it issued for?"""
    token = (await request.form()).get("token", "")
    at = access_tokens.get(token)
    if at is None or at["exp"] < time.time() or families[at["family"]]["revoked"]:
        return {"active": False}
    return {"active": True, "aud": at["resource"]}


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
    return {"counters": counters, "families": families, "issue_count": len(issues),
            "registered": [{k: v for k, v in r.items() if k != "client_secret"}
                           for r in registered.values()]}


@app.post("/_test/revoke_family")
async def test_revoke_family():
    """Simulate 'user revoked at vendor' — next refresh gets invalid_grant."""
    for fam in families.values():
        fam["revoked"] = True
    return {"revoked_families": len(families)}


@app.post("/_test/at_ttl")
async def test_at_ttl(request: Request):
    """Set the access-token lifetime for tokens minted from now on, so a
    suite can use long-lived tokens (the broker's cache path)."""
    global AT_TTL
    AT_TTL = int((await request.json())["seconds"])
    return {"at_ttl": AT_TTL}


@app.post("/_test/reset")
async def test_reset():
    for store in (codes, families, refresh_tokens, access_tokens):
        store.clear()
    issues.clear()
    for k in counters:
        counters[k] = 0
    return {"ok": True}
