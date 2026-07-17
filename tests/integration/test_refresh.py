"""Refresh discipline (design §8/§9): single-flight under forced concurrency
(60s vendor tokens land every resolve inside the buffer), generation CAS,
STALE lifecycle including the mass-STALE page, and scope enforcement.

Ported from the lab's phase5 gate 3 + §8 tests and phase7 P2/P5.
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from stack import (
    MOCK,
    broker_audit,
    do_consent,
    mint,
    mock_reset,
    mock_state,
    resolve,
    revoke_grant,
)


@pytest.fixture(autouse=True, scope="module")
def _fresh_vendor(alice):
    """Start the module from a clean vendor + no grant."""
    mock_reset()
    revoke_grant(alice)
    yield


def test_twenty_parallel_resolves_one_vendor_refresh(alice):
    do_consent(alice)
    time.sleep(1)  # let the consent's token age past any in-flight work
    before = mock_state()["counters"]

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: resolve(alice), range(20)))

    assert all(r.status_code == 200 for r in results), \
        [r.status_code for r in results]
    tokens = {r.json()["access_token"] for r in results}
    assert len(tokens) == 1, "waiters must receive the winner's token"

    after = mock_state()["counters"]
    extra_refreshes = after["token_refresh"] - before["token_refresh"]
    assert extra_refreshes == 1, f"expected exactly one vendor refresh, saw {extra_refreshes}"
    assert after["rt_replay"] == 0, "a replay means the token family was burned"

    refreshes = broker_audit("broker.refresh")
    assert refreshes, "expected a broker.refresh audit event"
    last = refreshes[-1]
    assert last["generation_to"] == last["generation_from"] + 1


def test_generation_advances_on_next_refresh(alice):
    from stack import sub_of
    mine = [e for e in broker_audit("broker.refresh") if e.get("sub") == sub_of(alice)]
    gen_before = mine[-1]["generation_to"]
    r = resolve(alice)
    assert r.status_code == 200
    mine = [e for e in broker_audit("broker.refresh") if e.get("sub") == sub_of(alice)]
    assert mine[-1]["generation_to"] == gen_before + 1


def test_vendor_side_revocation_goes_stale_then_reconsent(alice):
    do_consent(alice)
    requests.post(f"{MOCK}/_test/revoke_family", timeout=10)
    r = resolve(alice)   # refresh hits invalid_grant
    assert r.status_code == 404 and "authorize_uri" in r.json()
    assert broker_audit("broker.stale")
    do_consent(alice)               # dance again -> fresh gen=1 entry
    assert resolve(alice).status_code == 200


def test_stale_storm_marks_each_entry_and_audits():
    """Uninstall (revoke_family) → every affected entry goes STALE and each
    transition is audited joinably by sub (per-entry behavior holds)."""
    mock_reset()
    tokens = [mint(f"wf-storm-{i}") for i in range(3)]
    for t in tokens:
        revoke_grant(t)
        do_consent(t)
    assert len(mock_state()["families"]) >= 3
    requests.post(f"{MOCK}/_test/revoke_family", timeout=10)
    for t in tokens:
        assert resolve(t).status_code == 404
    stale = broker_audit("broker.stale", since="3m")
    subs = {e.get("sub") for e in stale}
    assert len(subs & {f"wf-storm-{i}" for i in range(3)}) >= 3, \
        f"expected ≥3 distinct STALE subs, got {subs}"


def test_stale_storm_raises_mass_stale_signal():
    """≥3 STALEs for one vendor inside the window → a distinct page event."""
    events = broker_audit("broker.stale.mass")
    if not events:  # drive a storm if the previous test's window has passed
        mock_reset()
        tokens = [mint(f"wf-mass-{i}") for i in range(3)]
        for t in tokens:
            revoke_grant(t)
            do_consent(t)
        requests.post(f"{MOCK}/_test/revoke_family", timeout=10)
        for t in tokens:
            resolve(t)
        events = broker_audit("broker.stale.mass")
    assert events, "no mass-stale/page anomaly signal emitted"
    assert events[-1].get("page") is True
    assert events[-1].get("security_event") is True
    for t_index in range(3):  # leave no stale grants behind
        revoke_grant(mint(f"wf-mass-{t_index}"))
        revoke_grant(mint(f"wf-storm-{t_index}"))


def test_scope_ceiling_cannot_be_exceeded(alice):
    """required_scopes beyond the registry ceiling → 403, never a consent."""
    r = resolve(alice, required_scopes=["issues:read", "admin:everything"])
    assert r.status_code == 403
    body = r.json()
    assert body["title"] == "scope-exceeds-ceiling"
    assert "admin:everything" in body["required"]


def test_narrow_grant_gets_409_reconsent_with_missing_scopes():
    """An ACTIVE grant narrower than required_scopes → 409 needs-reconsent-scope
    with missing_scopes and a step-up authorize_uri (design §4.1)."""
    sub = "wf-narrow"
    tok = mint(sub)
    revoke_grant(tok)
    # Consent for issues:read only.
    r = resolve(tok, required_scopes=["issues:read"])
    assert r.status_code == 404
    page = requests.get(r.json()["authorize_uri"], timeout=15)
    assert page.status_code == 200 and "Connected" in page.text

    # Ask for wider scopes while the grant still holds only issues:read.
    # (Any refresh first would widen it — the mock is scope-sloppy on the
    # refresh grant, GitHub-class behavior.)
    r = resolve(tok, required_scopes=["issues:read", "issues:write"])
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["title"] == "needs-reconsent-scope"
    assert body["missing_scopes"] == ["issues:write"]
    assert "/v1/authorize/mockhub" in body["authorize_uri"]

    # The step-up dance unions held+required and then resolves cleanly.
    page = requests.get(body["authorize_uri"], timeout=15)
    assert page.status_code == 200 and "Connected" in page.text
    assert resolve(tok, required_scopes=["issues:read", "issues:write"]).status_code == 200
    revoke_grant(tok)
