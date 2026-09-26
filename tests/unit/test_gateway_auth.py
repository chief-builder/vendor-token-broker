"""Gateway configuration and its OAuth protected-resource wiring."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.server.auth.providers.jwt import JWTVerifier
from gateway_helpers import make_gateway, make_gateway_config
from starlette.testclient import TestClient

from mcp_gateway.config import ConfigError, GatewayConfig
from mcp_gateway.server import build_auth, build_server

CFG = make_gateway_config()
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_PEM = KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-11-25", "capabilities": {},
    "clientInfo": {"name": "t", "version": "0"}}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _token(aud=CFG.resource_url, scope="openid mcp-gateway", alg="PS256", **over) -> str:
    now = int(time.time())
    claims = {"iss": CFG.hub_issuer, "sub": "alice", "aud": aud, "scope": scope,
              "iat": now, "exp": now + 300, **over}
    return jwt.encode(claims, KEY, algorithm=alg)


@pytest.fixture(scope="module")
def http():
    gw, *_ = make_gateway()
    verifier = JWTVerifier(public_key=PUBLIC_PEM, issuer=CFG.hub_issuer,
                           audience=CFG.resource_url, algorithm="PS256",
                           required_scopes=["mcp-gateway"])
    app = build_server(gw, auth=build_auth(CFG, verifier)).http_app(path="/mcp")
    with TestClient(app, base_url="http://localhost:8500") as c:
        yield c


def test_protected_resource_metadata_names_the_hub_and_scope(http):
    r = http.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    body = r.json()
    assert body["resource"] == CFG.resource_url
    assert body["authorization_servers"] == [CFG.hub_issuer]
    assert body["scopes_supported"] == ["mcp-gateway"]


def test_unauthenticated_request_gets_the_discovery_challenge(http):
    r = http.post("/mcp", json=INIT, headers=HEADERS)
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert "resource_metadata=" in challenge and 'scope="mcp-gateway"' in challenge


@pytest.mark.parametrize("token", [
    _token(aud="http://elsewhere/mcp"),               # meant for another resource
    _token(scope="openid"),                           # gateway scope not granted
    _token(alg="RS256"),                              # algorithm not pinned
    _token(iss="http://attacker/realms/mcp"),         # another issuer
    _token(exp=int(time.time()) - 60),                # expired
])
def test_bad_tokens_are_rejected(http, token):
    r = http.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_valid_token_reaches_the_mcp_endpoint(http):
    r = http.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200, r.text


ENV = {"GATEWAY_PUBLIC_URL": "http://localhost:8500", "HUB_ISSUER": "http://h/realms/mcp",
       "HUB_JWKS_URI": "http://h/certs", "HUB_TOKEN_ENDPOINT": "http://h/token",
       "GATEWAY_CLIENT_ID": "mcp-gateway", "GATEWAY_CLIENT_SECRET": "s",
       "BROKER_URL": "http://broker:8300/"}


def test_config_defaults_are_read_only_github():
    cfg = GatewayConfig.from_env(ENV)
    assert cfg.resource_url == "http://localhost:8500/mcp"
    assert cfg.broker_url == "http://broker:8300"
    assert cfg.upstream_readonly and cfg.upstream_lockdown
    assert "issue_read" in cfg.upstream_tools and cfg.vendor == "github"


def test_config_reads_overrides():
    cfg = GatewayConfig.from_env({**ENV, "UPSTREAM_TOOLS": "get_me, list_issues",
                                  "UPSTREAM_READONLY": "false", "MIN_TTL_S": "60"})
    assert cfg.upstream_tools == ("get_me", "list_issues")
    assert cfg.upstream_readonly is False and cfg.min_ttl_s == 60


@pytest.mark.parametrize("env, message", [
    ({k: v for k, v in ENV.items() if k != "HUB_ISSUER"}, "HUB_ISSUER"),
    ({**ENV, "HUB_ALGORITHM": "RS256"}, "not allowed"),
    ({**ENV, "UPSTREAM_TOOLS": " , "}, "at least one tool"),
    ({**ENV, "UPSTREAM_READONLY": "maybe"}, "true or false"),
    ({**ENV, "MIN_TTL_S": "soon"}, "invalid literal"),
])
def test_config_fails_fast(env, message):
    with pytest.raises(ConfigError, match=message):
        GatewayConfig.from_env(env)
