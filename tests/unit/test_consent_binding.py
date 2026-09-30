"""Consent is bound to the user AND the browser (review H1, design §6).

Before the fix, anyone holding an authorize link could complete consent:
an attacker could send their own link to a victim and have the victim's
vendor account stored under the attacker's sub. Now /v1/authorize sends the
browser to log in at the hub (the logged-in sub must be the link's sub) and
binds every leg to the browser that opened the link."""

import time

import pytest
from broker_harness import VENDOR, Harness, audit_events, query

from token_broker.consent import binding_cookie
from token_broker.hub import HubUnavailable
from token_broker.hub_login import HubLoginError


def security_fails(capsys, reason):
    return [
        e
        for e in audit_events(capsys, "broker.consent.fail")
        if e.get("reason") == reason and e.get("security_event") is True
    ]


# ------------------------------------------------------------ the authorize link


async def test_authorize_sends_the_browser_to_the_hub_bound_to_it():
    h = Harness()
    r = await h.authorize("wf-user-1")
    assert r.status_code in (302, 307)
    q = query(r.headers["location"])
    assert r.headers["location"].startswith(h.hub_login.AUTHORIZE)
    assert q["login_hint"] == "wf-user-1"
    assert q["redirect_uri"] == "http://broker.test:8300/v1/callback/_hub"
    assert q["nonce"] and q["code_challenge"]
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{binding_cookie(VENDOR)}=")
    for attribute in ("HttpOnly", "Path=/v1/callback", "SameSite=lax"):
        assert attribute in cookie
    assert "Secure" not in cookie  # http public URL in this config


async def test_real_idp_mode_sends_no_login_hint():
    """HUB_LOGIN_HINT=none: an opaque sub is no username hint for a real IdP."""
    h = Harness(hub_login_hint="none")
    r = await h.authorize("wf-user-1")
    assert "login_hint" not in query(r.headers["location"])


async def test_binding_cookie_is_secure_behind_https():
    h = Harness(broker_public_url="https://broker.example")
    r = await h.authorize()
    assert "Secure" in r.headers["set-cookie"]


async def test_an_authorize_link_starts_one_flow_only():
    h = Harness()
    txn = await h.broker.new_txn("wf-user-1", VENDOR, ["issues:read"])
    first = await h.browser.get(f"/v1/authorize/{VENDOR}", params={"txn": txn})
    second = await h.browser.get(f"/v1/authorize/{VENDOR}", params={"txn": txn})
    assert first.status_code in (302, 307)
    assert second.status_code == 400 and second.json()["title"] == "invalid-transaction"


async def test_an_authorize_link_expires_after_five_minutes():
    h = Harness()
    txn = await h.broker.new_txn("wf-user-1", VENDOR, ["issues:read"])
    (await h.coord.get_txn(txn))["created_at"] = time.time() - 301
    r = await h.browser.get(f"/v1/authorize/{VENDOR}", params={"txn": txn})
    assert r.status_code == 400 and r.json()["title"] == "invalid-transaction"


async def test_hub_unreachable_at_authorize_is_503():
    h = Harness()

    async def down(**kw):
        raise HubUnavailable("hub login metadata unavailable")

    h.hub_login.authorization_url = down
    r = await h.authorize()
    assert r.status_code == 503 and r.json()["title"] == "hub-unavailable"


# ------------------------------------------------------------ the attacks


@pytest.mark.parametrize(
    "link_for,opened_by",
    [
        ("wf-attacker", "wf-victim"),  # attacker forwards their own link to a victim
        ("wf-victim", "wf-attacker"),  # attacker opens a link stolen from a victim
    ],
)
async def test_a_link_opened_by_another_user_never_reaches_the_vendor(link_for, opened_by, capsys):
    h = Harness()
    h.hub_login.login_as = opened_by
    r = await h.hub_callback(await h.authorize(link_for))
    assert r.status_code == 403
    assert "location" not in r.headers  # no vendor redirect
    [fail] = security_fails(capsys, "login_sub_mismatch")
    assert fail["sub"] == link_for and fail["login_sub"] == opened_by
    assert h.stored(link_for) is None and h.stored(opened_by) is None


async def test_hub_leg_completed_in_another_browser_is_rejected(capsys):
    """Login CSRF: an attacker's own hub callback forwarded to a victim."""
    h = Harness()
    started = await h.authorize("wf-attacker")
    other_browser = h.client()
    async with other_browser:
        r = await h.hub_callback(started, browser=other_browser)
    assert r.status_code == 400
    assert security_fails(capsys, "browser_mismatch")


async def test_vendor_leg_completed_in_another_browser_never_redeems(capsys):
    h = Harness()
    state = await h.start_consent()
    async with h.client() as other_browser:
        r = await other_browser.get(
            f"/v1/callback/{VENDOR}", params={"state": state, "code": "c", "iss": "http://as.test"}
        )
    assert r.status_code == 400
    assert h.vendors.exchange_calls == 0
    assert security_fails(capsys, "browser_mismatch")
    assert (await h.callback(state)).status_code == 200  # the real browser still can


# ------------------------------------------------------------ state misuse


async def test_hub_state_is_single_use(capsys):
    h = Harness()
    started = await h.authorize()
    assert (await h.hub_callback(started)).status_code in (302, 307)
    assert (await h.hub_callback(started)).status_code == 400
    assert security_fails(capsys, "state_invalid_or_replayed")


async def test_hub_state_cannot_be_used_on_the_vendor_callback():
    h = Harness()
    hub_state = query((await h.authorize()).headers["location"])["state"]
    r = await h.callback(hub_state)
    assert r.status_code == 400 and h.vendors.exchange_calls == 0


async def test_vendor_state_cannot_be_used_on_the_hub_callback():
    h = Harness()
    vendor_state = await h.start_consent()
    r = await h.browser.get("/v1/callback/_hub", params={"state": vendor_state, "code": "x"})
    assert r.status_code == 400


async def test_hub_issuer_mismatch_is_rejected(capsys):
    h = Harness()
    q = query((await h.authorize()).headers["location"])
    r = await h.browser.get(
        "/v1/callback/_hub",
        params={"state": q["state"], "code": "code-for-wf-user-1", "iss": "https://evil.example"},
    )
    assert r.status_code == 400
    assert security_fails(capsys, "iss_mismatch")


async def test_failed_hub_login_is_502_and_audited(capsys):
    h = Harness()
    h.hub_login.exchange_error = HubLoginError("ID token nonce mismatch")
    r = await h.hub_callback(await h.authorize())
    assert r.status_code == 502
    [fail] = [
        e
        for e in audit_events(capsys, "broker.consent.fail")
        if e.get("reason") == "hub_login_failed"
    ]
    assert fail["error"] == "ID token nonce mismatch"


async def test_full_consent_still_works():
    h = Harness()
    assert (await h.callback(await h.start_consent())).status_code == 200
    assert h.stored()["access_token"] == "at-consent"
