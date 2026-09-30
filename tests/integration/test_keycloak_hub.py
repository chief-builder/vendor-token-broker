"""Keycloak as the real hub (gateway profile): the MCP token a client gets
from Keycloak is exchanged (RFC 8693) for a hub JWT the unchanged broker
accepts, and the broker's consent leg signs users in at Keycloak."""

import pytest
import requests
from keycloak_stack import (
    USERS,
    consent_via_keycloak,
    exchange,
    hub_jwt,
    jwt_part,
    mcp_token,
    register_client,
    resolve_kc,
    revoke_kc,
)
from stack import grep_container_logs

pytestmark = pytest.mark.gateway


def test_exchanged_token_matches_the_hub_contract():
    token = hub_jwt("alice")
    header, claims = jwt_part(token, 0), jwt_part(token, 1)
    assert header["alg"] == "PS256"
    assert claims["sub"] == USERS["alice"]
    assert claims["aud"] == "mcp://tier/internal"  # exactly one tier, nothing else
    assert claims["mcp_contract"] == "1.0"
    assert claims["azp"] == "mcp-gateway"  # who performed the exchange
    assert claims["exp"] - claims["iat"] <= 300
    assert claims["jti"]


def test_mcp_token_is_bound_to_the_gateway():
    claims = jwt_part(mcp_token("alice"), 1)
    assert claims["sub"] == USERS["alice"]
    assert "http://localhost:8500/mcp" in claims["aud"]
    assert "mcp://tier/internal" not in claims["aud"]


def test_broker_accepts_the_exchanged_token():
    token = hub_jwt("alice")
    revoke_kc(token)
    r = resolve_kc(token)
    assert r.status_code == 404, r.text  # authenticated, not yet connected
    assert r.json()["title"] == "needs-consent"


def test_broker_rejects_the_raw_mcp_token():
    """No token passthrough: the MCP token is never a broker credential."""
    r = resolve_kc(mcp_token("alice"))
    assert r.status_code == 401, r.text


def test_exchange_requires_a_token_meant_for_the_gateway():
    r = exchange(mcp_token("alice", scope="openid"))  # no mcp-gateway audience
    assert r.status_code in (400, 403), r.text


def test_exchange_requires_the_gateway_credential():
    r = exchange(mcp_token("alice"), client_secret="wrong")
    assert r.status_code == 401, r.text


def test_consent_signs_in_at_keycloak_and_connects():
    token = hub_jwt("alice")
    revoke_kc(token)
    page = consent_via_keycloak(resolve_kc(token).json()["authorize_uri"], "alice")
    assert page.status_code == 200 and "Connected" in page.text, page.text[:300]
    assert resolve_kc(token).status_code == 200


def test_forwarded_link_signed_in_as_someone_else_is_refused():
    token = hub_jwt("alice")
    revoke_kc(token)
    page = consent_via_keycloak(resolve_kc(token).json()["authorize_uri"], "bob")
    assert page.status_code == 403, page.text[:300]
    assert resolve_kc(token).status_code == 404  # nothing was connected


def test_no_token_material_in_any_container_log():
    token = hub_jwt("alice")
    r = resolve_kc(token)
    if r.status_code == 404:
        consent_via_keycloak(r.json()["authorize_uri"], "alice")
        r = resolve_kc(token)
    assert r.status_code == 200, r.text
    for secret in (r.json()["access_token"], token):
        assert grep_container_logs(secret, since="10m") == {}


# -------------------------------------- stock MCP clients (dynamic registration)


def test_stock_client_can_self_register_with_a_localhost_redirect():
    r = register_client()
    assert r.status_code == 201, r.text
    assert r.json()["client_id"]


def test_self_registration_is_refused_for_other_redirects():
    assert register_client("https://attacker.example/callback").status_code == 403


def test_self_registered_clients_token_is_accepted_by_the_gateway():
    client_id = register_client().json()["client_id"]
    token = mcp_token(
        "alice",
        scope="mcp-gateway offline_access",
        client_id=client_id,
        redirect_uri="http://localhost:33419/callback",
    )
    claims = jwt_part(token, 1)
    assert claims["sub"] == USERS["alice"]
    assert "http://localhost:8500/mcp" in claims["aud"]
    r = requests.post(
        "http://localhost:8500/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=15,
    )
    assert r.status_code == 200, r.text
