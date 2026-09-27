"""Refresh correctness (review change 6): min_ttl_s clamping and serving the
winner's token (M4), non-expiring and refresh-token-less grants (M7), the
REVOKE_PENDING interplay with re-consent and waiting resolves (L3), deletes
serialized against refreshes (L4), and ceiling-capped recorded scopes (L10)."""
import asyncio
import time

import pytest
from broker_harness import VENDOR, Harness, audit_events

from token_broker import sweeper
from token_broker import vendors as vendors_mod
from token_broker.refresh import NON_EXPIRING_S, entry_from_token_response

# ------------------------------------------------------------ M4: min_ttl_s


@pytest.mark.parametrize("coord", ["memory", "redis"])
async def test_min_ttl_longer_than_vendor_tokens_still_single_flight(coord, capsys):
    """Review M4 repro: 10 callers want 120s, the vendor issues 60s tokens.
    Before: 10 serial vendor refreshes. Now: one, and the waiters get the
    winner's (short) token."""
    h = Harness(coord=coord)
    h.put(expires_at=time.time() + 100)
    h.vendors.refresh_delay = 0.05
    results = await asyncio.gather(*[h.resolve(min_ttl_s=120) for _ in range(10)])
    assert {r.status_code for r in results} == {200}
    assert {r.json()["access_token"] for r in results} == {"at-1"}
    assert h.vendors.refresh_calls == 1
    waited = [e for e in audit_events(capsys, "broker.resolve")
              if e["path"] == "refresh-waited"]
    assert len(waited) == 9 and all(e["short_ttl"] is True for e in waited)


async def test_min_ttl_above_the_buffer_is_clamped_and_audited(capsys):
    h = Harness()                                   # REFRESH_BUFFER_S = 300
    h.put(expires_at=time.time() + 400)             # > buffer, < requested 600
    r = await h.resolve(min_ttl_s=600)
    assert r.status_code == 200 and r.json()["access_token"] == "at-0"
    assert h.vendors.refresh_calls == 0             # served, not force-refreshed
    [event] = audit_events(capsys, "broker.resolve")
    assert event["path"] == "cache" and event["min_ttl_clamped_from"] == 600


async def test_waiter_never_refreshes_an_entry_parked_for_revocation():
    """A delete parks the entry while a resolve waits for the lock: the
    resolve must not refresh it back to ACTIVE (that undid the revocation)."""
    h = Harness()
    h.put(expires_at=time.time() + 100)
    held, _ = await h.coord.wait_refresh_lock(VENDOR, "wf-user-1", None)
    waiting = asyncio.create_task(h.resolve())
    await asyncio.sleep(0.05)
    h.put(expires_at=time.time() + 100, state="REVOKE_PENDING")
    await h.coord.release_refresh_lock(VENDOR, "wf-user-1", held)
    r = await waiting
    assert r.status_code == 409 and r.json()["title"] == "revoke-pending"
    assert h.vendors.refresh_calls == 0
    assert h.stored()["state"] == "REVOKE_PENDING"


# ------------------------------------------------------------ M7: token shapes

def test_token_shapes_map_to_expiry():
    now = time.time()
    never = entry_from_token_response({"access_token": "a"}, 1, "u", [])
    assert never["expires_at"] >= now + NON_EXPIRING_S - 5 and never["refresh_token"] == ""
    no_rt = entry_from_token_response({"access_token": "a", "expires_in": 60}, 1, "u", [])
    assert no_rt["expires_at"] <= now + 61
    no_exp = entry_from_token_response({"access_token": "a", "refresh_token": "r"}, 1, "u", [])
    assert now + 8 * 3600 - 5 <= no_exp["expires_at"] <= now + 8 * 3600 + 5
    kept = entry_from_token_response({"access_token": "a", "expires_in": 60}, 2, "u", [],
                                      previous_refresh_token="rt-old")
    assert kept["refresh_token"] == "rt-old"
    assert kept["expires_at"] <= now + 61       # an RT from before: not "never expires"


async def test_non_expiring_grant_is_never_refreshed():
    """Review M7: no expires_in and no refresh_token (a GitHub App token with
    expiry disabled). Before: forced STALE every 8h. Now: served from custody
    indefinitely; neither resolve nor the sweeper tries to refresh it."""
    h = Harness()
    h.vendors.consent_token = {"access_token": "at-forever", "scope": "issues:read"}
    assert (await h.callback(await h.start_consent())).status_code == 200
    r = await h.resolve(min_ttl_s=300)
    assert r.status_code == 200 and r.json()["access_token"] == "at-forever"
    await sweeper.sweep_once(h.broker)
    assert h.vendors.refresh_calls == 0
    assert h.stored()["state"] == "ACTIVE"


async def test_expired_grant_without_refresh_token_goes_stale_without_a_vendor_call(capsys):
    h = Harness()
    h.put(refresh_token="", expires_at=time.time() + 100)
    r = await h.resolve()
    assert r.status_code == 404 and r.json()["title"] == "needs-consent"
    assert h.vendors.refresh_calls == 0          # never sends an empty refresh token
    assert h.stored()["state"] == "STALE"
    assert audit_events(capsys, "broker.stale")
    count, _ = await h.coord.record_stale(VENDOR)
    assert count == 1                             # not counted as an uninstall signal


# ------------------------------------------------------------ L3: re-consent


async def test_reconsent_revokes_a_grant_parked_for_revocation_first(capsys):
    h = Harness()
    h.put(state="REVOKE_PENDING", refresh_token="rt-parked")
    r = await h.callback(await h.start_consent())
    assert r.status_code == 200
    assert [e["refresh_token"] for e in h.vendors.revoked] == ["rt-parked"]
    assert h.stored()["state"] == "ACTIVE" and h.stored()["access_token"] == "at-consent"
    revoke = audit_events(capsys, "broker.revoke")
    assert revoke[-1]["outcome"] == "revoked" and revoke[-1]["path"] == "reconsent"


async def test_reconsent_waits_while_the_parked_revocation_cannot_complete():
    h = Harness()
    h.put(state="REVOKE_PENDING", refresh_token="rt-parked")
    h.vendors.revoke_error = vendors_mod.VendorUnavailable("vendor revocation unavailable")
    r = await h.callback(await h.start_consent())
    assert r.status_code == 503
    assert h.vendors.exchange_calls == 0          # no new grant was minted
    assert h.stored()["state"] == "REVOKE_PENDING"


async def test_reconsent_over_an_active_grant_does_not_revoke_it():
    """Revoking the predecessor could kill the new grant at vendors that
    revoke per user and client, so ACTIVE and STALE are simply overwritten."""
    h = Harness()
    h.put(state="ACTIVE")
    assert (await h.callback(await h.start_consent())).status_code == 200
    assert h.vendors.revoked == []
    assert h.stored()["access_token"] == "at-consent"


async def test_consent_writes_under_the_entry_lock():
    """The sweeper's revocation retry holds this lock, so a consent can
    never land between its version check and its delete."""
    h = Harness()
    state = await h.start_consent()
    token = await h.coord.try_refresh_lock(VENDOR, "wf-user-1")
    callback = asyncio.create_task(h.callback(state))
    await asyncio.sleep(0.05)
    assert not callback.done() and h.stored() is None
    await h.coord.release_refresh_lock(VENDOR, "wf-user-1", token)
    assert (await callback).status_code == 200
    assert h.stored()["access_token"] == "at-consent"


async def test_consent_still_stores_the_grant_if_the_lock_never_frees():
    """The code is already spent: losing the new grant is worse than waiting."""
    h = Harness(lock_timeout_s=1)
    state = await h.start_consent()
    await h.coord.try_refresh_lock(VENDOR, "wf-user-1")          # never released
    assert (await h.callback(state)).status_code == 200
    assert h.stored()["access_token"] == "at-consent"


# ------------------------------------------------------------ L4: delete vs refresh


async def _delete(h):
    async with h.client() as c:
        return await c.delete(f"/v1/grants/{VENDOR}/wf-user-1",
                              headers={"Authorization": f"Bearer {h.token()}"})


async def test_delete_waits_for_an_in_flight_refresh_and_revokes_the_new_pair():
    h = Harness()
    h.put(expires_at=time.time() + 100)
    h.vendors.refresh_delay = 0.1
    refreshing = asyncio.create_task(h.resolve())
    await asyncio.sleep(0.02)                      # the refresh holds the lock
    r = await _delete(h)
    assert (await refreshing).status_code == 200
    assert r.status_code == 200
    assert [e["refresh_token"] for e in h.vendors.revoked] == ["rt-1"]   # not rt-0
    assert h.stored() is None


async def test_delete_times_out_cleanly_when_the_lock_is_held():
    h = Harness(lock_timeout_s=1)
    h.put()
    held, _ = await h.coord.wait_refresh_lock(VENDOR, "wf-user-1", None)
    try:
        r = await _delete(h)
    finally:
        await h.coord.release_refresh_lock(VENDOR, "wf-user-1", held)
    assert r.status_code == 503 and r.json()["title"] == "vendor-unavailable"
    assert h.vendors.revoke_calls == 0 and h.stored() is not None


async def test_delete_revokes_a_pair_that_landed_after_a_lost_lock():
    h = Harness()
    h.put(refresh_token="rt-old")
    landed = []

    def newer_pair_lands_once():
        if not landed:
            landed.append(True)
            h.put(refresh_token="rt-newer", refresh_generation=2)

    h.vendors.on_revoke = newer_pair_lands_once
    r = await _delete(h)
    assert r.status_code == 200
    assert [e["refresh_token"] for e in h.vendors.revoked] == ["rt-old", "rt-newer"]
    assert h.stored() is None


# ------------------------------------------------------------ L10: scope cap


async def test_refresh_records_only_scopes_within_the_ceiling(capsys):
    h = Harness()
    h.put(expires_at=time.time() + 100, granted_scopes=["issues:read"])
    h.vendors.refresh_scope = "issues:read issues:write admin:org"
    assert (await h.resolve()).status_code == 200
    assert h.stored()["granted_scopes"] == ["issues:read", "issues:write"]
    [refresh] = audit_events(capsys, "broker.refresh")
    assert refresh["scope_widened"] == ["admin:org"]


async def test_consent_records_only_scopes_within_the_ceiling(capsys):
    h = Harness()
    h.vendors.consent_token = {"access_token": "at-c", "refresh_token": "rt-c",
                               "expires_in": 3600, "scope": "issues:read repo:delete"}
    assert (await h.callback(await h.start_consent())).status_code == 200
    assert h.stored()["granted_scopes"] == ["issues:read"]
    [complete] = audit_events(capsys, "broker.consent.complete")
    assert complete["scope_widened"] == ["repo:delete"]


async def test_empty_ceiling_records_the_vendor_scopes_as_given():
    h = Harness()
    h.vendors.spec["scope_ceiling"] = []
    h.put(expires_at=time.time() + 100, granted_scopes=[])
    h.vendors.refresh_scope = "repo workflow"
    assert (await h.resolve()).status_code == 200
    assert h.stored()["granted_scopes"] == ["repo", "workflow"]
