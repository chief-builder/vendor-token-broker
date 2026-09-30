"""Security-relevant broker paths, each with its positive and negative case:
RFC 9207 issuer checks on the vendor callback, single-use state, no
reflected vendor errors, fail-closed coordination on every consent and
DELETE path, self-service DELETE, and the grant listing."""

import pytest
from broker_harness import VENDOR, Harness, audit_events

from token_broker.coordination import CoordinationUnavailable


async def down(*args, **kwargs):
    raise CoordinationUnavailable()


def security_fails(capsys, reason: str) -> list[dict]:
    return [
        e
        for e in audit_events(capsys, "broker.consent.fail")
        if e.get("reason") == reason and e.get("security_event") is True
    ]


# ------------------------------------------------ vendor callback: RFC 9207 iss


async def test_callback_with_the_recorded_issuer_completes():
    h = Harness()
    state = await h.start_consent()
    r = await h.callback(state, iss="http://as.test")
    assert r.status_code == 200
    assert h.stored()["access_token"] == "at-consent"


@pytest.mark.parametrize("iss", [None, "http://evil.test"])
async def test_missing_or_foreign_issuer_is_rejected_before_redemption(capsys, iss):
    """The AS advertises iss support, so a callback without iss is a mix-up
    signal exactly like a foreign one. The code is never redeemed and the
    state survives for the legitimate callback."""
    h = Harness()
    state = await h.start_consent()
    params = {"state": state, "code": "code-1"}
    if iss is not None:
        params["iss"] = iss
    r = await h.browser.get(f"/v1/callback/{VENDOR}", params=params)
    assert r.status_code == 400 and "Issuer mismatch" in r.text
    assert h.vendors.exchange_calls == 0
    assert h.stored() is None
    [event] = security_fails(capsys, "iss_mismatch")
    assert event["iss_present"] is (iss is not None)

    legit = await h.callback(state, iss="http://as.test")
    assert legit.status_code == 200


# ---------------------------------------------------------- single-use state


async def test_a_vendor_state_is_redeemed_once(capsys):
    h = Harness()
    state = await h.start_consent()
    assert (await h.callback(state)).status_code == 200
    replay = await h.callback(state)
    assert replay.status_code == 400
    assert h.vendors.exchange_calls == 1
    assert security_fails(capsys, "state_invalid_or_replayed")


async def test_losing_the_consumption_race_is_a_replay(capsys):
    """Two callbacks pass the peek; only one may consume and redeem."""
    h = Harness()
    state = await h.start_consent()

    async def lost(state):
        return None

    h.coord.consume_state = lost
    r = await h.callback(state)
    assert r.status_code == 400
    assert h.vendors.exchange_calls == 0
    assert security_fails(capsys, "state_invalid_or_replayed")


async def test_vendor_error_is_not_reflected_to_the_browser(capsys):
    h = Harness()
    state = await h.start_consent()
    hostile = "<script>alert(1)</script>"
    r = await h.callback(state, error=hostile)
    assert r.status_code == 400
    assert hostile not in r.text and "script" not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert h.vendors.exchange_calls == 0
    [event] = audit_events(capsys, "broker.consent.fail")
    assert event["reason"] == hostile and event["security_event"] is False


# ----------------------------------------- fail closed when coordination is down


async def test_authorize_fails_closed_without_coordination():
    h = Harness()
    txn = await h.broker.new_txn("wf-user-1", VENDOR, ["issues:read"])
    h.coord.take_txn = down
    r = await h.browser.get(f"/v1/authorize/{VENDOR}", params={"txn": txn})
    assert r.status_code == 503 and r.json()["title"] == "coordination-unavailable"
    assert "set-cookie" not in r.headers


async def test_authorize_fails_closed_when_the_state_cannot_be_stored():
    h = Harness()
    h.coord.put_state = down
    r = await h.authorize()
    assert r.status_code == 503 and r.json()["title"] == "coordination-unavailable"


async def test_hub_callback_fails_closed_without_coordination():
    h = Harness()
    authorize = await h.authorize()
    h.coord.peek_state = down
    r = await h.hub_callback(authorize)
    assert r.status_code == 503 and "Coordination store unavailable" in r.text


async def test_vendor_leg_fails_closed_when_its_state_cannot_be_stored():
    h = Harness()
    authorize = await h.authorize()
    h.coord.put_state = down
    r = await h.hub_callback(authorize)
    assert r.status_code == 503 and r.json()["title"] == "coordination-unavailable"


async def test_vendor_callback_fails_closed_without_coordination():
    h = Harness()
    state = await h.start_consent()
    h.coord.peek_state = down
    r = await h.callback(state)
    assert r.status_code == 503 and "Coordination store unavailable" in r.text
    assert h.vendors.exchange_calls == 0


async def test_delete_fails_closed_without_coordination():
    h = Harness()
    h.put()
    h.coord.wait_refresh_lock = down
    async with h.client() as c:
        r = await c.delete(
            f"/v1/grants/{VENDOR}/wf-user-1",
            headers={"Authorization": f"Bearer {h.token()}"},
        )
    assert r.status_code == 503 and r.json()["title"] == "coordination-unavailable"
    assert h.vendors.revoke_calls == 0 and h.stored() is not None


async def test_cache_hits_still_serve_without_coordination():
    """Only the paths that need coordination fail; a live token is served."""
    h = Harness()
    h.put()  # expires in an hour: no refresh needed
    h.coord.wait_refresh_lock = down
    r = await h.resolve()
    assert r.status_code == 200 and r.json()["access_token"] == "at-0"


async def test_refresh_fails_closed_without_coordination():
    import time

    h = Harness()
    h.put(expires_at=time.time() + 10)  # inside the refresh buffer
    h.coord.wait_refresh_lock = down
    r = await h.resolve()
    assert r.status_code == 503 and r.json()["title"] == "coordination-unavailable"
    assert h.vendors.refresh_calls == 0


# ------------------------------------------------------ self-service DELETE


async def delete(h: Harness, path_sub: str, as_sub: str = "wf-user-1"):
    async with h.client() as c:
        return await c.delete(
            f"/v1/grants/{VENDOR}/{path_sub}",
            headers={"Authorization": f"Bearer {h.token(as_sub)}"},
        )


async def test_a_user_deletes_their_own_grant_vendor_first():
    h = Harness()
    h.put()
    r = await delete(h, "wf-user-1")
    assert r.status_code == 200 and r.json() == {"revoked": True}
    assert h.vendors.revoke_calls == 1 and h.stored() is None


async def test_a_user_cannot_delete_someone_elses_grant():
    h = Harness()
    h.put(sub="victim")
    r = await delete(h, "victim", as_sub="attacker")
    assert r.status_code == 403 and r.json()["title"] == "forbidden"
    assert h.vendors.revoke_calls == 0 and h.stored("victim") is not None


async def test_delete_without_a_hub_token_is_401():
    h = Harness()
    h.put()
    async with h.client() as c:
        r = await c.delete(f"/v1/grants/{VENDOR}/wf-user-1")
    assert r.status_code == 401 and h.stored() is not None


async def test_a_uri_shaped_subject_deletes_its_own_grant():
    h = Harness()
    h.put(sub="tenant/alice")
    r = await delete(h, "tenant/alice", as_sub="tenant/alice")
    assert r.status_code == 200 and h.stored("tenant/alice") is None


# ----------------------------------------------------------- GET /v1/grants


async def list_grants(h: Harness, as_sub: str = "wf-user-1", auth: bool = True):
    headers = {"Authorization": f"Bearer {h.token(as_sub)}"} if auth else {}
    async with h.client() as c:
        return await c.get("/v1/grants", headers=headers)


async def test_grants_list_shows_the_callers_grant_without_tokens():
    h = Harness()
    h.put()
    r = await list_grants(h)
    assert r.status_code == 200
    [grant] = r.json()["grants"]
    assert grant["vendor"] == VENDOR and grant["state"] == "ACTIVE"
    assert set(grant) == {"vendor", "state", "granted_scopes", "vendor_user_id", "created_at"}
    assert "at-0" not in r.text and "rt-0" not in r.text


async def test_grants_list_hides_the_internal_refreshing_state():
    h = Harness()
    h.put(state="REFRESHING")
    [grant] = (await list_grants(h)).json()["grants"]
    assert grant["state"] == "ACTIVE"


@pytest.mark.parametrize("state", ["STALE", "REVOKE_PENDING"])
async def test_grants_list_reports_unusable_states(state):
    h = Harness()
    h.put(state=state)
    [grant] = (await list_grants(h)).json()["grants"]
    assert grant["state"] == state


async def test_grants_list_shows_only_the_callers_grants():
    h = Harness()
    h.put(sub="someone-else")
    assert (await list_grants(h)).json() == {"grants": []}


async def test_grants_list_needs_a_hub_token():
    h = Harness()
    h.put()
    r = await list_grants(h, auth=False)
    assert r.status_code == 401 and r.json()["title"] == "invalid-hub-token"


async def test_grants_list_fails_closed_when_custody_is_down():
    h = Harness()
    h.put()
    h.custody.fail = True
    r = await list_grants(h)
    assert r.status_code == 503 and r.json()["title"] == "vault-unavailable"
