"""hub-stub — a stand-in workforce IdP for the acceptance suite.

Exposes exactly two things: the JWKS the broker pins its validation to, and
a /_test/token mint that issues hub-shaped JWTs (PS256/ES256 with the
contract claims) plus deliberately-bad variants (RS256 on a resolvable key,
wrong issuer/tier/contract, expired, missing jti) so the hub-validation
matrix runs against a *deployed* broker.

This is test infrastructure. It is NOT part of the broker, which remains a
custodian with no issuance surface.
"""
import json
import os
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI, Request
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

ISSUER = os.environ.get("HUB_STUB_ISSUER", "http://hub-stub:8320")
TIER = os.environ.get("HUB_STUB_TIER_AUDIENCE", "mcp://tier/internal")
CONTRACT = os.environ.get("HUB_STUB_CONTRACT", "1.0")

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
