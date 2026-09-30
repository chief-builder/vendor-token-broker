"""Vendors whose MCP server runs its own sign-in (registry `resource`,
RFC 8707): the mockhub-atlassian and mockhub-cloudflare entries point at
mock-vendor's MCP authorization server, which binds every code, refresh-token
family and access token to the resource sent on authorize and answers
invalid_target when a code exchange or refresh doesn't repeat it."""

import requests
from stack import MOCK, do_consent, mint, mock_state, resolve, revoke_grant

RESOURCE = {
    "mockhub-atlassian": "http://mock-mcp:8330/atlassian/mcp",
    "mockhub-cloudflare": "http://mock-mcp:8330/cloudflare/mcp",
}


def _audience(access_token: str) -> str | None:
    return (
        requests.post(f"{MOCK}/introspect", data={"token": access_token}, timeout=10)
        .json()
        .get("aud")
    )


def test_tokens_are_issued_for_the_vendors_mcp_server_across_refreshes():
    tok = mint("wf-resource")
    before = mock_state()["counters"]
    for vendor, resource in RESOURCE.items():
        revoke_grant(tok, vendor)
        do_consent(tok, vendor)  # authorize + code exchange
        for _ in range(2):  # 60s tokens: each resolve refreshes
            r = resolve(tok, vendor)
            assert r.status_code == 200, r.text
            assert _audience(r.json()["access_token"]) == resource
    after = mock_state()["counters"]
    assert after["invalid_target"] == before["invalid_target"]
    assert after["token_refresh"] >= before["token_refresh"] + 4


def test_ordinary_vendors_get_tokens_for_no_particular_server():
    tok = mint("wf-resource")
    do_consent(tok)
    assert _audience(resolve(tok).json()["access_token"]) is None
