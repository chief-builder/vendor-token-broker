"""Gateway configuration and its OAuth protected-resource wiring."""
import json
import logging
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from gateway_helpers import make_gateway, make_gateway_config
from starlette.testclient import TestClient

from mcp_gateway.config import ConfigError, GatewayConfig, UpstreamSpec, load_upstreams
from mcp_gateway.server import AuditingJWTVerifier, build_app, build_auth
from mcp_gateway.upstream import Upstream

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
    verifier = AuditingJWTVerifier(public_key=PUBLIC_PEM, issuer=CFG.hub_issuer,
                           audience=CFG.resource_url, algorithm="PS256",
                           required_scopes=["mcp-gateway"])
    app = build_app(gw, build_auth(CFG, verifier))
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


@pytest.mark.parametrize("token, reason", [
    (_token(aud="http://elsewhere/mcp"), "audience"),       # meant for another resource
    (_token(scope="openid"), "scope"),                      # gateway scope not granted
    (_token(alg="RS256"), "algorithm"),                     # algorithm not pinned
    (_token(iss="http://attacker/realms/mcp"), "issuer"),   # another issuer
    (_token(exp=int(time.time()) - 60), "expired"),         # expired
    ("not-a-jwt", "malformed"),
])
def test_bad_tokens_are_rejected_and_the_reason_audited(http, token, reason, caplog):
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    r = http.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    denials = [json.loads(m) for m in caplog.messages if '"gateway.auth"' in m]
    assert denials and denials[-1]["reason"] == reason
    assert token not in caplog.text


def test_request_without_a_bearer_token_is_audited_as_missing(http, caplog):
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    assert http.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
    assert json.loads([m for m in caplog.messages if '"gateway.auth"' in m][-1])["reason"] \
        == "missing"


def test_forged_signature_is_audited_as_signature(http, caplog):
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = jwt.encode({"iss": CFG.hub_issuer, "sub": "alice", "aud": CFG.resource_url,
                         "scope": "mcp-gateway", "iat": now, "exp": now + 300},
                        other, algorithm="PS256")
    r = http.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    assert json.loads([m for m in caplog.messages if '"gateway.auth"' in m][-1])["reason"] \
        == "signature"


def test_valid_token_reaches_the_mcp_endpoint(http):
    r = http.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200, r.text


ENV = {"GATEWAY_PUBLIC_URL": "http://localhost:8500", "HUB_ISSUER": "http://h/realms/mcp",
       "HUB_JWKS_URI": "http://h/certs", "HUB_TOKEN_ENDPOINT": "http://h/token",
       "GATEWAY_CLIENT_ID": "mcp-gateway", "GATEWAY_CLIENT_SECRET": "s",
       "BROKER_URL": "http://broker:8300/"}


def test_bundled_upstreams_are_read_only_github_and_linear():
    cfg = GatewayConfig.from_env(ENV)
    assert cfg.resource_url == "http://localhost:8500/mcp"
    assert cfg.broker_url == "http://broker:8300"
    upstreams = {u.name: u for u in cfg.upstreams}
    assert list(upstreams) == ["github", "linear"]
    assert upstreams["github"].headers["X-MCP-Readonly"] == "true"
    assert upstreams["github"].headers["X-MCP-Lockdown"] == "true"
    assert upstreams["linear"].url.endswith("/mcp/readonly")


def test_bundled_snapshots_match_their_allowlists_and_are_read_only():
    for u in load_upstreams():
        assert [t.name for t in u.snapshot] == list(u.tools), u.name
        assert all(t.annotations and t.annotations.read_only_hint for t in u.snapshot), u.name


def test_enabled_upstreams_filter_the_list():
    cfg = GatewayConfig.from_env({**ENV, "GATEWAY_ENABLED_UPSTREAMS": "linear",
                                  "MIN_TTL_S": "60"})
    assert [u.name for u in cfg.upstreams] == ["linear"] and cfg.min_ttl_s == 60


def _write(tmp_path, upstreams: list[dict], snapshot: dict | None = None) -> str:
    if snapshot is not None:
        (tmp_path / "snap.json").write_text(json.dumps(snapshot))
    path = tmp_path / "upstreams.json"
    path.write_text(json.dumps({"upstreams": upstreams}))
    return str(path)


UP = {"name": "sentry", "display_name": "Sentry", "vendor": "sentry",
      "url": "https://mcp.sentry.dev/mcp", "tools": ["search_issues"],
      "auth_scheme": "Sentry-Bearer"}


def test_custom_upstreams_file_with_a_relative_snapshot(tmp_path):
    tool = {"name": "search_issues", "description": "d", "inputSchema": {"type": "object"}}
    path = _write(tmp_path, [{**UP, "snapshot": "./snap.json"}], {"tools": [tool]})
    [u] = GatewayConfig.from_env({**ENV, "GATEWAY_UPSTREAMS": path}).upstreams
    assert (u.name, u.auth_scheme, [t.name for t in u.snapshot]) == \
        ("sentry", "Sentry-Bearer", ["search_issues"])


@pytest.mark.parametrize("upstreams, message", [
    ([UP, UP], "unique"),
    ([{**UP, "name": "Sentry"}], "lowercase"),
    ([{**UP, "name": "my_sentry"}], "lowercase"),
    ([{**UP, "tools": []}], "at least one tool"),
    ([{**UP, "auth_scheme": "Basic"}], "auth_scheme"),
    ([{**UP, "protocol": "2024-11-05"}], "protocol"),
    ([{**UP, "headers": {"authorization": "x"}}], "Authorization"),
    ([{k: v for k, v in UP.items() if k != "vendor"}], "vendor"),
    ([], "no upstreams"),
])
def test_bad_upstreams_files_fail_fast(tmp_path, upstreams, message):
    path = _write(tmp_path, upstreams)
    with pytest.raises(ConfigError, match=message):
        GatewayConfig.from_env({**ENV, "GATEWAY_UPSTREAMS": path})


@pytest.mark.parametrize("env, message", [
    ({k: v for k, v in ENV.items() if k != "HUB_ISSUER"}, "HUB_ISSUER"),
    ({**ENV, "HUB_ALGORITHM": "RS256"}, "not allowed"),
    ({**ENV, "MIN_TTL_S": "soon"}, "invalid literal"),
    ({**ENV, "GATEWAY_ENABLED_UPSTREAMS": "github,jira"}, "unknown upstreams"),
    ({**ENV, "GATEWAY_UPSTREAMS": "/nonexistent/upstreams.json"}, "upstreams file"),
])
def test_config_fails_fast(env, message):
    with pytest.raises(ConfigError, match=message):
        GatewayConfig.from_env(env)


@pytest.mark.parametrize("scheme", ["Bearer", "Sentry-Bearer"])
def test_upstream_sends_its_scheme_and_headers(scheme):
    spec = UpstreamSpec(name="x", display_name="X", vendor="x", url="http://x/mcp",
                        tools=("t",), auth_scheme=scheme, headers={"X-MCP-Readonly": "true"})
    headers = Upstream(spec, 5)._client("tok").transport.headers
    assert headers == {"X-MCP-Readonly": "true", "Authorization": f"{scheme} tok"}
