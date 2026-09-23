"""The resolve state machine and refresh core (design §7/§8/§9), offline.

Locks in current behavior so later fixes land against regression tests:
cache hit, lazy single-flight refresh, generation CAS (the loser serves the
winner's token and never writes its own pair), STALE on invalid_grant with
the mass-STALE page, VendorDown leaving the entry usable, persisted
REFRESHING (fresh vs abandoned) on the redis profile, lock timeout, and
fail-closed custody."""
import asyncio
import time

import pytest
from broker_harness import VENDOR, Harness, audit_events

from token_broker import vendors as vendors_mod


def _paths(events):
    return [e.get("path") for e in events if e["audit"] == "broker.resolve"]


# ------------------------------------------------------------ steady state

async def test_cache_path_serves_without_refresh(capsys):
    h = Harness()
    h.put(expires_at=time.time() + 3600)
    r = await h.resolve()
    assert r.status_code == 200
    assert r.json()["access_token"] == "at-0"
    assert h.vendors.refresh_calls == 0
    assert _paths(audit_events(capsys)) == ["cache"]


async def test_absent_entry_needs_consent_with_ceiling_txn():
    h = Harness()
    r = await h.resolve()
    assert r.status_code == 404
    body = r.json()
    assert body["title"] == "needs-consent"
    txn = body["authorize_uri"].split("txn=")[1]
    record = await h.coord.get_txn(txn)
    assert record["sub"] == "wf-user-1" and record["scopes"] == h.vendors.spec["scope_ceiling"]


async def test_stale_entry_needs_consent():
    h = Harness()
    h.put(state="STALE")
    r = await h.resolve()
    assert r.status_code == 404 and r.json()["title"] == "needs-consent"


async def test_revoke_pending_is_plain_409():
    h = Harness()
    h.put(state="REVOKE_PENDING")
    r = await h.resolve()
    assert r.status_code == 409
    assert r.json()["title"] == "revoke-pending"
    assert "authorize_uri" not in r.json()


async def test_unknown_vendor_404():
    h = Harness()
    r = await h.resolve(vendor="nope")
    assert r.status_code == 404 and r.json()["title"] == "unknown-vendor"


async def test_sub_mismatch_400():
    h = Harness()
    r = await h.resolve(sub="someone-else")
    assert r.status_code == 400 and r.json()["title"] == "sub-mismatch"


async def test_required_scopes_beyond_ceiling_403():
    h = Harness()
    r = await h.resolve(required_scopes=["issues:read", "admin:org"])
    assert r.status_code == 403 and r.json()["title"] == "scope-exceeds-ceiling"


async def test_narrow_grant_409_reconsent_unions_scopes():
    h = Harness()
    h.put(granted_scopes=["issues:read"])
    r = await h.resolve(required_scopes=["issues:write"])
    assert r.status_code == 409
    body = r.json()
    assert body["title"] == "needs-reconsent-scope"
    assert body["missing_scopes"] == ["issues:write"]
    record = await h.coord.get_txn(body["authorize_uri"].split("txn=")[1])
    assert record["scopes"] == ["issues:read", "issues:write"]


# ------------------------------------------------------------ lazy refresh

async def test_lazy_refresh_inside_buffer_advances_generation(capsys):
    h = Harness()
    h.put(expires_at=time.time() + 100)          # < REFRESH_BUFFER_S (300)
    r = await h.resolve()
    assert r.status_code == 200 and r.json()["access_token"] == "at-1"
    stored = h.stored()
    assert stored["refresh_generation"] == 2
    assert stored["refresh_token"] == "rt-1"     # rotated RT persisted
    assert h.vendors.refresh_rts == ["rt-0"]
    events = audit_events(capsys)
    refresh = [e for e in events if e["audit"] == "broker.refresh"]
    assert [(e["generation_from"], e["generation_to"]) for e in refresh] == [(1, 2)]
    assert _paths(events) == ["refreshed"]


@pytest.mark.parametrize("coord", ["memory", "redis"])
async def test_concurrent_resolves_single_flight(coord):
    h = Harness(coord=coord)
    h.put(expires_at=time.time() + 100)
    h.vendors.refresh_delay = 0.05
    results = await asyncio.gather(*[h.resolve() for _ in range(10)])
    assert {r.status_code for r in results} == {200}
    assert {r.json()["access_token"] for r in results} == {"at-1"}
    assert h.vendors.refresh_calls == 1
    assert h.stored()["refresh_generation"] == 2


async def test_non_rotating_vendor_keeps_existing_refresh_token():
    h = Harness()
    h.vendors.rotate = False
    h.put(expires_at=time.time() + 100)
    assert (await h.resolve()).status_code == 200
    assert h.stored()["refresh_token"] == "rt-0"


async def test_cas_loser_serves_winner_and_never_writes_its_pair(capsys):
    h = Harness()
    h.put(expires_at=time.time() + 100)

    def other_writer(_n):   # another writer lands between our read and our CAS
        h.put(access_token="at-other", refresh_token="rt-other",
              refresh_generation=7, expires_at=time.time() + 3600)

    h.vendors.on_refresh = other_writer
    r = await h.resolve()
    assert r.status_code == 200 and r.json()["access_token"] == "at-other"
    stored = h.stored()
    assert stored["access_token"] == "at-other"
    assert stored["refresh_token"] == "rt-other"
    assert stored["refresh_generation"] == 7
    events = audit_events(capsys)
    assert "cas-lost" in _paths(events)
    assert not [e for e in events if e["audit"] == "broker.refresh"]


# ------------------------------------------------------------ STALE

async def test_invalid_grant_goes_stale_and_needs_consent(capsys):
    h = Harness()
    h.put(expires_at=time.time() + 100)
    h.vendors.refresh_error = vendors_mod.InvalidGrant("invalid_grant")
    r = await h.resolve()
    assert r.status_code == 404 and r.json()["title"] == "needs-consent"
    assert h.stored()["state"] == "STALE"
    stale = audit_events(capsys, "broker.stale")
    assert [(e["sub"], e["generation"]) for e in stale] == [("wf-user-1", 1)]


async def test_mass_stale_pages_once_at_threshold(capsys):
    h = Harness()
    h.vendors.refresh_error = vendors_mod.InvalidGrant("invalid_grant")
    for i in range(4):
        h.put(sub=f"u{i}", expires_at=time.time() + 100)
        assert (await h.resolve(as_sub=f"u{i}")).status_code == 404
    events = audit_events(capsys)
    assert len([e for e in events if e["audit"] == "broker.stale"]) == 4
    mass = [e for e in events if e["audit"] == "broker.stale.mass"]
    assert len(mass) == 1
    assert mass[0]["count"] == 3 and mass[0]["page"] is True


# ------------------------------------------------------------ vendor outage

async def test_vendor_down_503_entry_untouched_memory():
    h = Harness()
    h.put(expires_at=time.time() + 100)
    h.vendors.refresh_error = vendors_mod.VendorUnavailable("vendor token endpoint 503")
    r = await h.resolve()
    assert r.status_code == 503 and r.json()["title"] == "vendor-unavailable"
    stored = h.stored()
    assert stored["state"] == "ACTIVE" and stored["refresh_generation"] == 1
    assert h.custody.entries[(VENDOR, "wf-user-1")][1] == 1   # no write at all


async def test_vendor_down_restores_active_redis():
    """Redis profile persists REFRESHING first; a known vendor failure
    restores ACTIVE instead of waiting out the takeover TTL."""
    h = Harness(coord="redis")
    h.put(expires_at=time.time() + 100)
    h.vendors.refresh_error = vendors_mod.VendorUnavailable("vendor token endpoint 503")
    r = await h.resolve()
    assert r.status_code == 503
    stored = h.stored()
    assert stored["state"] == "ACTIVE"
    assert stored["refresh_token"] == "rt-0" and stored["refresh_generation"] == 1


# ------------------------------------------------------------ persisted REFRESHING

async def test_redis_refresh_success_leaves_no_marker():
    h = Harness(coord="redis")
    h.put(expires_at=time.time() + 100)
    assert (await h.resolve()).status_code == 200
    stored = h.stored()
    assert stored["state"] == "ACTIVE" and stored["refresh_generation"] == 2


async def test_fresh_refreshing_marker_is_never_rerefreshed(capsys):
    h = Harness(coord="redis")
    h.put(state="REFRESHING", refresh_owner="other-replica",
          refresh_started_at=time.time(), expires_at=time.time() + 100)
    r = await h.resolve()
    assert r.status_code == 200 and r.json()["access_token"] == "at-0"
    assert h.vendors.refresh_calls == 0
    assert "refresh-in-progress" in _paths(audit_events(capsys))


async def test_fresh_refreshing_marker_too_short_to_serve_is_503():
    h = Harness(coord="redis")
    h.put(state="REFRESHING", refresh_owner="other-replica",
          refresh_started_at=time.time(), expires_at=time.time() + 10)
    r = await h.resolve(min_ttl_s=30)
    assert r.status_code == 503 and r.json()["title"] == "vendor-unavailable"
    assert h.vendors.refresh_calls == 0


async def test_abandoned_refreshing_marker_is_taken_over():
    h = Harness(coord="redis")
    h.put(state="REFRESHING", refresh_owner="dead-replica",
          refresh_started_at=time.time() - 60,        # > REFRESHING_TTL_S (30)
          expires_at=time.time() + 10)
    r = await h.resolve()
    assert r.status_code == 200 and r.json()["access_token"] == "at-1"
    stored = h.stored()
    assert stored["state"] == "ACTIVE" and stored["refresh_generation"] == 2


# ------------------------------------------------------------ lock timeout

async def test_lock_timeout_rereads_and_serves_usable_token(capsys):
    h = Harness(lock_timeout_s=1)
    h.put(expires_at=time.time() + 100)
    held, _ = await h.coord.wait_refresh_lock(VENDOR, "wf-user-1", None)
    try:
        r = await h.resolve()
    finally:
        await h.coord.release_refresh_lock(VENDOR, "wf-user-1", held)
    assert r.status_code == 200 and r.json()["access_token"] == "at-0"
    assert h.vendors.refresh_calls == 0
    assert "lock-timeout-reread" in _paths(audit_events(capsys))


async def test_lock_timeout_with_unusable_token_is_503():
    h = Harness(lock_timeout_s=1)
    h.put(expires_at=time.time() + 10)
    held, _ = await h.coord.wait_refresh_lock(VENDOR, "wf-user-1", None)
    try:
        r = await h.resolve(min_ttl_s=30)
    finally:
        await h.coord.release_refresh_lock(VENDOR, "wf-user-1", held)
    assert r.status_code == 503 and r.json()["title"] == "vendor-unavailable"


# ------------------------------------------------------------ custody outage

async def test_custody_outage_fails_closed_uncached():
    h = Harness()
    h.custody.fail = True
    r = await h.resolve()
    assert r.status_code == 503 and r.json()["title"] == "vault-unavailable"


async def test_custody_outage_still_serves_cache_hit():
    h = Harness()
    h.put(expires_at=time.time() + 3600)
    assert (await h.resolve()).status_code == 200    # warms the cache
    h.custody.fail = True
    r = await h.resolve()
    assert r.status_code == 200 and r.json()["access_token"] == "at-0"
