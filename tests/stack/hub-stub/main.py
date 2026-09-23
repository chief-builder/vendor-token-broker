"""hub-stub — a stand-in workforce IdP for the acceptance suite.

Exposes exactly two things: the JWKS the broker pins its validation to, and
a /_test/token mint that issues hub-shaped JWTs (PS256/ES256 with the
contract claims) plus deliberately-bad variants (RS256 on a resolvable key,
wrong issuer/tier/contract, expired, missing jti) so the hub-validation
matrix runs against a *deployed* broker.

It also plays the hub's OIDC login for the broker's consent leg (review
H1): discovery, /authorize (auto-login as the login_hint, or as the user set
via /_test/login_as), and /token (PKCE-checked, returns a signed ID token).

This is test infrastructure. It is NOT part of the broker, which remains a
custodian with no issuance surface.
"""
import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from urllib.parse import parse_qs, urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

ISSUER = os.environ.get("HUB_STUB_ISSUER", "http://hub-stub:8320")
TIER = os.environ.get("HUB_STUB_TIER_AUDIENCE", "mcp://tier/internal")
CONTRACT = os.environ.get("HUB_STUB_CONTRACT", "1.0")
PUBLIC_URL = os.environ.get("HUB_STUB_PUBLIC_URL", "http://localhost:8320")
LOGIN_CLIENT_ID = os.environ.get("HUB_STUB_LOGIN_CLIENT_ID", "vtb-broker")
LOGIN_CLIENT_SECRET = os.environ.get("HUB_STUB_LOGIN_CLIENT_SECRET", "hub-login-secret")
login_codes: dict[str, dict] = {}
login_as: dict[str, str | None] = {"sub": None}   # /_test/login_as override

app = FastAPI(title="hub-stub")

_rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ec = ec.generate_private_key(ec.SECP256R1())


def _jwk(public_key, kid: str) -> dict:
    algo = RSAAlgorithm(RSAAlgorithm.SHA256) if isinstance(
        public_key, rsa.RSAPublicKey) else ECAlgorithm(ECAlgorithm.SHA256)
    d = json.loads(algo.to_jwk(public_key))
    d.update({"kid": kid, "use": "sig"})
    return d


@app.get("/jwks")
async def jwks():
    return {"keys": [_jwk(_rsa.public_key(), "hub-rsa"),
                     _jwk(_ec.public_key(), "hub-ec")]}


@app.post("/_test/token")
async def mint(request: Request):
    """Mint a hub JWT. Body: {sub, groups?, kind?, ttl_s?}. kind selects the
    good shape (ps256 default, es256) or a deliberately-bad variant."""
    raw = await request.body()
    body = json.loads(raw) if raw else {}
    kind = body.get("kind", "ps256")
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": body.get("sub", "wf-user-1"),
        "aud": [TIER],
        "exp": now + int(body.get("ttl_s", 3600)),
        "iat": now,
        "jti": str(uuid.uuid4()),
        "mcp_contract": CONTRACT,
    }
    if body.get("groups"):
        claims["groups"] = body["groups"]
    key, alg, kid = _rsa, "PS256", "hub-rsa"
    if kind == "es256":
        key, alg, kid = _ec, "ES256", "hub-ec"
    elif kind == "rs256":
        alg = "RS256"  # forbidden by the broker's pin; key still resolvable
    elif kind == "wrong_issuer":
        claims["iss"] = "https://evil.example"
    elif kind == "external_tier":
        claims["aud"] = ["mcp://tier/external"]
    elif kind == "two_tiers":
        claims["aud"] = [TIER, "mcp://tier/external"]
    elif kind == "expired":
        claims["exp"], claims["iat"] = now - 600, now - 1200
    elif kind == "no_jti":
        claims.pop("jti")
    elif kind == "wrong_contract":
        claims["mcp_contract"] = "0.9"
    elif kind == "no_contract":
        claims.pop("mcp_contract")
    elif kind != "ps256":
        return {"error": f"unknown kind {kind}"}
    token = jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})
    return {"access_token": token, "sub": claims["sub"], "kind": kind}


# ---------------------------------------------------------------- OIDC login

@app.get("/.well-known/openid-configuration")
async def openid_configuration():
    return {"issuer": ISSUER,
            "authorization_endpoint": f"{PUBLIC_URL}/authorize",   # browser-facing
            "token_endpoint": f"{ISSUER}/token",                   # broker-facing
            "jwks_uri": f"{ISSUER}/jwks",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "id_token_signing_alg_values_supported": ["PS256"]}


@app.get("/authorize")
async def authorize(client_id: str, redirect_uri: str, state: str, nonce: str,
                    code_challenge: str, code_challenge_method: str = "S256",
                    login_hint: str = "", response_type: str = "code", scope: str = ""):
    """Auto-login: the user is the login_hint, unless a test set login_as
    (simulating that someone else is signed in to this browser)."""
    if client_id != LOGIN_CLIENT_ID or response_type != "code" \
            or code_challenge_method != "S256":
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    sub = login_as["sub"] or login_hint
    code = f"hub-code-{secrets.token_urlsafe(16)}"
    login_codes[code] = {"sub": sub, "nonce": nonce, "redirect_uri": redirect_uri,
                         "challenge": code_challenge, "created_at": time.time()}
    return RedirectResponse(
        f"{redirect_uri}?{urlencode({'code': code, 'state': state, 'iss': ISSUER})}")


@app.post("/token")
async def token(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    if form.get("client_id") != LOGIN_CLIENT_ID or \
            form.get("client_secret") != LOGIN_CLIENT_SECRET:
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    rec = login_codes.pop(form.get("code", ""), None)
    if rec is None or time.time() - rec["created_at"] > 300 or \
            rec["redirect_uri"] != form.get("redirect_uri"):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
    if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != rec["challenge"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "pkce"},
                            status_code=400)
    now = int(time.time())
    id_token = jwt.encode(
        {"iss": ISSUER, "sub": rec["sub"], "aud": LOGIN_CLIENT_ID, "exp": now + 300,
         "iat": now, "nonce": rec["nonce"]},
        _rsa, algorithm="PS256", headers={"kid": "hub-rsa"})
    return {"id_token": id_token, "token_type": "Bearer", "expires_in": 300}


@app.post("/_test/login_as")
async def test_login_as(request: Request):
    """Set who is signed in to the hub (null: whoever the login_hint names)."""
    login_as["sub"] = (await request.json()).get("sub")
    return {"login_as": login_as["sub"]}
