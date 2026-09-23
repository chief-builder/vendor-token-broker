"""The maintenance sweeper (design §8), offline: proactive-band refresh,
REVOKE_PENDING retry, REFRESHING takeover, lock contention, error isolation,
and consent-record cleanup on the memory backend."""
import time

from broker_harness import VENDOR, Harness, audit_events

from token_broker import sweeper
from token_broker import vendors as vendors_mod


async def test_proactive_band_is_refreshed(capsys):
    h = Harness()
    h.put(expires_at=time.time() + 600)       # between buffer (300) and 900
    await sweeper.sweep_entry(h.broker, VENDOR, "wf-user-1")
    assert h.vendors.refresh_calls == 1
    assert h.stored()["refresh_generation"] == 2
    refresh = audit_events(capsys, "broker.refresh")
    assert refresh and refresh[0]["path"] == "proactive"


async def test_outside_the_band_is_left_alone():
    h = Harness()
    h.put(sub="far", expires_at=time.time() + 3600)    # not due yet
    h.put(sub="near", expires_at=time.time() + 100)    # lazy band: resolve's job
    h.put(sub="stale", state="STALE", expires_at=time.time() + 600)
    for sub in ("far", "near", "stale"):
        await sweeper.sweep_entry(h.broker, VENDOR, sub)
    assert h.vendors.refresh_calls == 0


async def test_proactive_invalid_grant_goes_stale():
    h = Harness()
    h.put(expires_at=time.time() + 600)
    h.vendors.refresh_error = vendors_mod.InvalidGrant("invalid_grant")
    await sweeper.sweep_entry(h.broker, VENDOR, "wf-user-1")
    assert h.stored()["state"] == "STALE"


async def test_revoke_pending_retry_revokes_and_deletes(capsys):
    h = Harness()
    h.put(state="REVOKE_PENDING")
    await sweeper.sweep_entry(h.broker, VENDOR, "wf-user-1")
    assert h.vendors.revoke_calls == 1
    assert h.stored() is None
    revoke = audit_events(capsys, "broker.revoke")
    assert revoke and revoke[0]["outcome"] == "revoked"
    assert revoke[0]["path"] == "sweep-retry"


async def test_revoke_pending_stays_parked_while_vendor_down():
    h = Harness()
    h.put(state="REVOKE_PENDING")
    h.vendors.revoke_error = vendors_mod.VendorUnavailable("revocation endpoint 503")
    await sweeper.sweep_entry(h.broker, VENDOR, "wf-user-1")
    assert h.stored()["state"] == "REVOKE_PENDING"


async def test_abandoned_refreshing_taken_over_fresh_one_skipped():
    h = Harness(coord="redis")
    h.put(sub="abandoned", state="REFRESHING", refresh_owner="dead",
          refresh_started_at=time.time() - 60, expires_at=time.time() + 3600)
    h.put(sub="fresh", state="REFRESHING", refresh_owner="live",
          refresh_started_at=time.time(), expires_at=time.time() + 600)
    await sweeper.sweep_entry(h.broker, VENDOR, "abandoned")
    await sweeper.sweep_entry(h.broker, VENDOR, "fresh")
    assert h.vendors.refresh_calls == 1
    assert h.stored("abandoned")["state"] == "ACTIVE"
    assert h.stored("fresh")["state"] == "REFRESHING"


async def test_entry_already_locked_is_skipped():
    h = Harness()
    h.put(expires_at=time.time() + 600)
    held = await h.coord.try_refresh_lock(VENDOR, "wf-user-1")
    try:
        await sweeper.sweep_entry(h.broker, VENDOR, "wf-user-1")
    finally:
        await h.coord.release_refresh_lock(VENDOR, "wf-user-1", held)
    assert h.vendors.refresh_calls == 0


async def test_bad_entry_is_audited_and_does_not_stop_the_pass(capsys):
    h = Harness()
    h.custody.write(VENDOR, "bad", {"state": "ACTIVE"})     # malformed entry
    h.put(sub="good", expires_at=time.time() + 600)
    await sweeper.sweep_once(h.broker)
    errors = audit_events(capsys, "broker.sweep.error")
    assert [e["sub"] for e in errors] == ["bad"]
    assert h.stored("good")["refresh_generation"] == 2


async def test_custody_outage_skips_the_vendor():
    h = Harness()
    h.put(expires_at=time.time() + 600)
    h.custody.fail = True
    await sweeper.sweep_once(h.broker)          # must not raise
    assert h.vendors.refresh_calls == 0


async def test_sweep_cleans_expired_memory_consent_records():
    h = Harness()
    await h.coord.put_txn("old", {"sub": "a", "vendor": VENDOR, "scopes": [],
                                  "created_at": time.time() - 601})
    await h.coord.put_txn("new", {"sub": "a", "vendor": VENDOR, "scopes": [],
                                  "created_at": time.time()})
    await sweeper.sweep_once(h.broker)
    assert set(h.coord._txns) == {"new"}
