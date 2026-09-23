"""Consent dance (design §6) and its defenses (§4.2/§4.3): the full
resolve→authorize→callback→resolve loop, sub binding, single-use state,
RFC 9207 iss tamper/omission, and the ceiling cap on the authorize leg.

Ported from the lab's phase5 gate 1 + phase7 P2/P3/P4 (Kong/MCP legs
dropped — the broker API is driven directly).
"""
from urllib.parse import parse_qs, urlparse

import requests
from stack import (
    BROKER,
    MOCK,
    broker_audit,
    do_consent,
    hub_login_as,
    mint,
    mock_state,
    new_consent_state,
    resolve,
    revoke_grant,
    sub_of,
    to_vendor,
    walk_to_callback,
)


def test_first_resolve_needs_consent_then_dance_then_200(alice):
    revoke_grant(alice)
    r = resolve(alice)
    assert r.status_code == 404
    authorize_uri = r.json()["authorize_uri"]
    assert "/v1/authorize/mockhub" in authorize_uri

    page = requests.get(authorize_uri, timeout=15)  # 302s: broker -> AS -> broker
    assert page.status_code == 200 and "Connected" in page.text

    r = resolve(alice)
    assert r.status_code == 200, r.text
    assert r.json()["access_token"].startswith("mock-at-")

    assert broker_audit("broker.consent.start")
    complete = broker_audit("broker.consent.complete")
    assert complete and complete[-1]["vendor_user_id"] == "mock-4217"


def test_consent_transaction_is_sub_bound(alice):
    """A forged/unknown txn is a clean 400, never a redirect."""
    do_consent(alice)
    assert resolve(alice).status_code == 200
    bad = requests.get(f"{BROKER}/v1/authorize/mockhub", params={"txn": "forged"},
                       timeout=10)
    assert bad.status_code == 400


def test_authorize_requests_only_the_ceiling(alice):
    """The authorize leg requests exactly the registry scope ceiling; the
    caller has no input that can widen it (design §4.2)."""
    _, location = to_vendor(alice)
    scope_param = parse_qs(urlparse(location).query).get("scope", [""])[0].split()
    ceiling = {"issues:read", "issues:write"}
    assert set(scope_param) <= ceiling, \
        f"broker requested {scope_param} beyond ceiling {ceiling}"
    do_consent(alice)  # leave the module with a usable grant


def test_state_replay_is_a_security_event(alice):
    browser, callback = walk_to_callback(alice)
    assert browser.get(callback, timeout=10).status_code == 200    # legit
    replay = browser.get(callback, timeout=10)                     # replayed
    assert replay.status_code == 400
    alerts = [e for e in broker_audit("broker.consent.fail")
              if e.get("security_event") and e.get("reason") == "state_invalid_or_replayed"]
    assert alerts, "state replay must raise a security alert"


def test_forged_state_is_a_security_event(alice):
    """A callback state that was never issued is the same alert (phase7 P3)."""
    r = requests.get(f"{BROKER}/v1/callback/mockhub",
                     params={"state": "never-issued", "code": "x"},
                     allow_redirects=False, timeout=15)
    assert r.status_code == 400
    alerts = [e for e in broker_audit("broker.consent.fail")
              if e.get("security_event") and e.get("reason") == "state_invalid_or_replayed"]
    assert alerts


def test_iss_tampering_never_redeems_the_code(alice):
    browser, callback = walk_to_callback(alice)
    redeemed_before = mock_state()["counters"]["token_code"]
    tampered = callback.replace("iss=http", "iss=https").replace(
        "mock-vendor%3A8310", "evil.example")
    r = browser.get(tampered, timeout=10)
    assert r.status_code == 400
    assert mock_state()["counters"]["token_code"] == redeemed_before
    alerts = [e for e in broker_audit("broker.consent.fail")
              if e.get("security_event") and e.get("reason") == "iss_mismatch"]
    assert alerts, "RFC 9207 mismatch must raise a security alert"
    assert browser.get(callback, timeout=10).status_code == 200   # untampered completes


def test_iss_omission_is_rejected_when_vendor_advertises_iss(alice):
    meta = requests.get(
        f"{MOCK}/.well-known/oauth-authorization-server", timeout=10).json()
    assert meta.get("authorization_response_iss_parameter_supported") is True
    browser, state = new_consent_state(alice)
    r = browser.get(f"{BROKER}/v1/callback/mockhub",
                     params={"state": state, "code": "forged-mixup-code"},
                     allow_redirects=False, timeout=15)
    assert r.status_code == 400 and "issuer" in r.text.lower(), \
        f"missing iss not rejected as a mix-up (got {r.status_code}: {r.text[:80]})"
    do_consent(alice)


def test_sub_mismatch_is_rejected(alice):
    r = resolve(alice, sub="somebody-else")
    assert r.status_code == 400


def test_consent_binds_to_the_initiating_sub(alice, bob):
    """The callback writes the entry under the txn's sub — bob completing
    alice's dance cannot attach the vendor account to bob."""
    do_consent(alice)
    revoke_grant(bob)
    assert resolve(bob).status_code == 404  # bob still unconnected
    assert resolve(alice, sub=sub_of(alice)).status_code == 200


# ── Consent bound to user and browser (review H1) ────────────────────────────

def test_link_forwarded_to_another_user_is_refused():
    """The attacker sends their own authorize link to a victim who opens it
    while signed in to the hub as themselves: refused before the vendor, so
    the victim's vendor account can never land under the attacker's sub."""
    attacker = mint("wf-attacker")
    revoke_grant(attacker)
    link = resolve(attacker).json()["authorize_uri"]
    authorized_before = mock_state()["counters"]["authorize"]
    hub_login_as("wf-victim")
    try:
        page = requests.get(link, timeout=15)          # the victim's browser
    finally:
        hub_login_as(None)
    assert page.status_code == 403 and "different user" in page.text
    assert mock_state()["counters"]["authorize"] == authorized_before   # never reached
    assert resolve(attacker).status_code == 404                        # nothing stored
    alerts = [e for e in broker_audit("broker.consent.fail")
              if e.get("reason") == "login_sub_mismatch" and e.get("security_event")]
    assert alerts and alerts[-1]["sub"] == "wf-attacker" \
        and alerts[-1]["login_sub"] == "wf-victim"


def test_vendor_leg_finished_in_another_browser_is_refused():
    """The vendor authorization URL, opened elsewhere: the vendor consents,
    but the callback lacks the binding cookie and no code is redeemed."""
    token = mint("wf-binding")
    _, location = to_vendor(token)
    redeemed_before = mock_state()["counters"]["token_code"]
    elsewhere = requests.get(location, timeout=15)     # fresh browser, follows redirects
    assert elsewhere.status_code == 400 and "different browser" in elsewhere.text
    assert mock_state()["counters"]["token_code"] == redeemed_before
    assert resolve(token).status_code == 404
    alerts = [e for e in broker_audit("broker.consent.fail")
              if e.get("reason") == "browser_mismatch" and e.get("security_event")]
    assert alerts


def test_authorize_link_is_single_use(alice):
    revoke_grant(alice)
    link = resolve(alice).json()["authorize_uri"]
    assert requests.get(link, timeout=15).status_code == 200    # full dance
    again = requests.get(link, timeout=15)
    assert again.status_code == 400 and again.json()["title"] == "invalid-transaction"
