"""Wire-compatibility freeze: the surface an existing gateway plugin
(plugins/vendor-token/handler.lua in the source lab) actually reads.

Frozen: status codes 200/404/409/5xx on resolve; response fields
`access_token`, `authorize_uri`, `missing_scopes`; problem `title` slugs
(read on plain 409); and the audit event-name vocabulary. If this file
fails, an existing deployment can NOT point its plugin at this broker.
"""
import subprocess

import requests
from stack import (
    BROKER,
    MOCK,
    MOCK_CONTAINER,
    broker_audit,
    do_consent,
    mint,
    mock_reset,
    resolve,
    revoke_grant,
    sub_of,
    wait_for,
)


def test_200_resolve_carries_frozen_fields(alice):
    do_consent(alice)
    r = resolve(alice)
    assert r.status_code == 200
    body = r.json()
    # handler.lua reads body.access_token verbatim.
    assert isinstance(body["access_token"], str) and body["access_token"]
    assert "expires_at" in body and "granted_scopes" in body


def test_404_needs_consent_carries_authorize_uri(alice):
    revoke_grant(alice)
    r = resolve(alice)
    assert r.status_code == 404
    body = r.json()
    # handler.lua: 404 + body.authorize_uri -> authorization_required challenge.
    assert body["authorize_uri"].startswith("http")
    assert body["title"] == "needs-consent"
    do_consent(alice)


def test_409_reconsent_carries_authorize_uri_and_missing_scopes():
    tok = mint("wf-wire-409")
    revoke_grant(tok)
    r = resolve(tok, required_scopes=["issues:read"])
    assert r.status_code == 404
    assert requests.get(r.json()["authorize_uri"], timeout=15).status_code == 200
    r = resolve(tok, required_scopes=["issues:read", "issues:write"])
    assert r.status_code == 409
    body = r.json()
    # handler.lua: 409 + authorize_uri -> step-up; reads missing via body.
    assert body["title"] == "needs-reconsent-scope"
    assert body["missing_scopes"] == ["issues:write"]
    assert body["authorize_uri"].startswith("http")
    revoke_grant(tok)


def test_plain_409_title_is_the_frozen_slug(alice):
    """handler.lua exits 403 with body.title on a plain 409 — the title slug
    `revoke-pending` must survive extraction byte-identically."""
    do_consent(alice)
    subprocess.run(["docker", "pause", MOCK_CONTAINER], check=True,
                   capture_output=True)
    try:
        # Vendor down -> RFC 7009 revoke fails -> entry parked REVOKE_PENDING.
        r = requests.delete(f"{BROKER}/v1/grants/mockhub/{sub_of(alice)}",
                            headers={"Authorization": f"Bearer {alice}"}, timeout=30)
        assert r.status_code == 502
        assert r.json()["title"] == "revoke-pending"
        r = resolve(alice)
        assert r.status_code == 409
        assert r.json()["title"] == "revoke-pending"       # the frozen slug
        assert "authorize_uri" not in r.json()             # plain 409, not step-up
    finally:
        subprocess.run(["docker", "unpause", MOCK_CONTAINER], check=True,
                       capture_output=True)
    wait_for(lambda: requests.get(f"{BROKER}/healthz", timeout=5).ok,
             timeout=30, what="broker healthy after mock unpause")
    # Cleanup: vendor is back; delete retries and succeeds.
    wait_for(lambda: requests.delete(
        f"{BROKER}/v1/grants/mockhub/{sub_of(alice)}",
        headers={"Authorization": f"Bearer {alice}"},
        timeout=15).status_code in (200, 404),
        timeout=30, what="revoke retry after vendor recovery")


def test_audit_event_names_are_frozen():
    """The SIEM joins on these names; the extraction must not rename them.
    Drive a full lifecycle, then assert every frozen name was emitted."""
    mock_reset()
    tok = mint("wf-wire-audit")
    revoke_grant(tok)
    r = resolve(tok)                                     # broker.resolve (needs-consent)
    assert r.status_code == 404
    assert requests.get(r.json()["authorize_uri"],       # consent.start/complete
                        timeout=15).status_code == 200
    assert resolve(tok).status_code == 200               # broker.refresh
    requests.post(f"{MOCK}/_test/revoke_family", timeout=10)
    assert resolve(tok).status_code == 404               # broker.stale
    r = resolve(tok)
    assert requests.get(r.json()["authorize_uri"], timeout=15).status_code == 200
    revoke_grant(tok)                                    # broker.revoke

    seen = {e["audit"] for e in broker_audit(since="3m")}
    frozen = {"broker.resolve", "broker.consent.start", "broker.consent.complete",
              "broker.refresh", "broker.stale", "broker.revoke"}
    assert frozen <= seen, f"missing audit events: {frozen - seen}"
