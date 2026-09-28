"""Storage and coordination hygiene (review change 7): consent-record
cleanup independent of the sweeper (M10), STALE scrubbing (M9), bounded
in-process tables and an atomic sweep lease (L5/L6), configurable timeouts
checked against the lock TTL (L7), registry ids safe as path segments, and
revocation that works for every grant shape."""
import asyncio
import json
import time

import httpx
import pytest
from broker_harness import VENDOR, Harness
from unit_helpers import MemoryCustody, make_config

from token_broker import coordination
from token_broker import main as main_mod
from token_broker import vendors as vendors_mod
from token_broker.config import Config, ConfigError
from token_broker.coordination import MemoryCoordination, RedisCoordination
from token_broker.vendors import VendorClient

# ------------------------------------------------------------ M10: cleanup timer


async def test_memory_records_are_reclaimed_with_the_sweeper_off(monkeypatch):
    monkeypatch.setattr(coordination, "CLEANUP_INTERVAL_S", 0.02)
    coord = MemoryCoordination(make_config(sweep_interval_s=0))
    await coord.put_txn("old", {"created_at": time.time() - 601})
    await coord.put_state("old-state", {"created_at": time.time() - 601})
    await coord.put_txn("new", {"created_at": time.time()})
    await coord.start(on_invalidate=lambda v, s: None)
    await asyncio.sleep(0.1)
    await coord.close()
    assert set(coord._txns) == {"new"} and coord._states == {}
    assert coord._cleanup_task.done()


async def test_old_page_markers_are_pruned():
    coord = MemoryCoordination(make_config(mass_stale_window_s=60))
    coord._paged_vendors = {"old": time.time() - 120, "recent": time.time()}
    await coord.cleanup()
    assert set(coord._paged_vendors) == {"recent"}


# ------------------------------------------------------------ L6: bounded tables


async def test_memory_locks_are_dropped_when_idle():
    coord = MemoryCoordination(make_config(lock_timeout_s=1))
    for i in range(50):
        token, _ = await coord.wait_refresh_lock(VENDOR, f"u{i}", None)
        await coord.release_refresh_lock(VENDOR, f"u{i}", token)
        token = await coord.try_refresh_lock(VENDOR, f"v{i}")
        await coord.release_refresh_lock(VENDOR, f"v{i}", token)
    assert coord._locks == {} and coord._lock_users == {}


async def test_memory_lock_survives_while_waiters_remain():
    coord = MemoryCoordination(make_config(lock_timeout_s=1))
    first, _ = await coord.wait_refresh_lock(VENDOR, "s", None)
    waiter = asyncio.create_task(coord.wait_refresh_lock(VENDOR, "s", None))
    await asyncio.sleep(0.01)
    await coord.release_refresh_lock(VENDOR, "s", first)
    second, _ = await waiter                   # the waiter got the same lock
    assert second is not None
    assert await coord.try_refresh_lock(VENDOR, "s") is None   # still held
    await coord.release_refresh_lock(VENDOR, "s", second)
    assert coord._locks == {}


async def test_memory_lock_timeout_leaves_no_residue():
    coord = MemoryCoordination(make_config(lock_timeout_s=1))
    held, _ = await coord.wait_refresh_lock(VENDOR, "s", None)
    assert (await coord.wait_refresh_lock(VENDOR, "s", None)) == (None, None)
    await coord.release_refresh_lock(VENDOR, "s", held)
    assert coord._locks == {} and coord._lock_users == {}


def test_token_cache_is_bounded_and_drops_expired(monkeypatch):
    monkeypatch.setattr(main_mod, "CACHE_MAX_ENTRIES", 5)
    b = Harness().broker
    for i in range(8):
        b.put_cache(VENDOR, f"u{i}", {"access_token": f"at-{i}"}, 1)
    assert list(b.cache) == [(VENDOR, f"u{i}") for i in range(3, 8)]   # oldest out
    for key in list(b.cache):                  # age every entry past the TTL
        entry, ver, _ = b.cache[key]
        b.cache[key] = (entry, ver, time.time() - b.cfg.cache_ttl_s - 1)
    b._cache_pruned_at = time.time() - b.cfg.cache_ttl_s
    b.put_cache(VENDOR, "fresh", {"access_token": "at-f"}, 1)
    assert list(b.cache) == [(VENDOR, "fresh")]


# ------------------------------------------------------------ L5: sweep lease


@pytest.fixture
def two_replicas():
    import fakeredis.aioredis
    cfg = make_config(sweep_interval_s=1)
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return (RedisCoordination(cfg, "inst-a", client=client),
            RedisCoordination(cfg, "inst-b", client=client))


async def test_only_the_holder_can_renew_the_lease(two_replicas):
    a, b = two_replicas
    assert await a.acquire_sweep_lease() is True
    assert await b.acquire_sweep_lease() is False
    await a._r.set("vtb:sweep-lease", "inst-b", px=2000)   # B took over meanwhile
    assert await a.acquire_sweep_lease() is False           # A must not extend it
    assert await a._r.get("vtb:sweep-lease") == "inst-b"


async def test_holder_renewal_extends_the_lease(two_replicas):
    a, _ = two_replicas
    assert await a.acquire_sweep_lease() is True
    await a._r.pexpire("vtb:sweep-lease", 50)
    assert await a.acquire_sweep_lease() is True
    assert await a._r.pttl("vtb:sweep-lease") > 1000


async def test_redis_close_waits_for_the_listener(two_replicas):
    a, _ = two_replicas
    await a.start(on_invalidate=lambda v, s: None)
    await asyncio.sleep(0.05)
    await a.close()
    assert a._listen_task.done()


# ------------------------------------------------------------ M9: STALE scrub


async def test_stale_entry_keeps_no_token_material():
    h = Harness()
    h.put(expires_at=time.time() + 100)
    h.vendors.refresh_error = vendors_mod.InvalidGrant("invalid_grant")
    assert (await h.resolve()).status_code == 404
    stored = h.stored()
    assert stored["state"] == "STALE"
    assert stored["access_token"] == "" and stored["refresh_token"] == ""
    assert stored["vendor_user_id"] == "vu-1"          # identity kept for audit


async def test_deleting_a_scrubbed_stale_grant_needs_no_vendor_call(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("revocation of a scrubbed entry must not call the vendor")

    monkeypatch.setattr(vendors_mod.httpx.AsyncClient, "post", no_network)
    await VendorClient(make_config(), MemoryCustody()).revoke(
        "mockhub", {"access_token": "", "refresh_token": ""})


# ------------------------------------------------------------ L7: timeouts


def test_timeout_knobs_parse():
    from test_config import FULL_ENV
    cfg = Config.from_env({**FULL_ENV, "VENDOR_TIMEOUT_S": "4", "JWKS_TIMEOUT_S": "2"})
    assert (cfg.vendor_timeout_s, cfg.jwks_timeout_s, cfg.lock_ttl_ms) == (4, 2, 20000)


def test_redis_lock_ttl_must_cover_the_slowest_refresh():
    with pytest.raises(ConfigError, match="LOCK_TTL_MS"):
        make_config(coord_backend="redis", lock_ttl_ms=15000)      # < (10 + 2x3) x 1000
    make_config(coord_backend="redis")                             # defaults pass
    make_config(coord_backend="redis", lock_ttl_ms=4000,
                vendor_timeout_s=2, vault_timeout_s=1)             # the multi test stack
    make_config(coord_backend="memory", lock_ttl_ms=1000)          # memory: no redis lock


async def test_vendor_calls_use_the_configured_timeout(monkeypatch):
    seen = []
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        seen.append(kwargs.get("timeout"))
        return real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))

    monkeypatch.setattr(vendors_mod.httpx, "AsyncClient", factory)
    client = VendorClient(make_config(vendor_timeout_s=4), MemoryCustody())
    await client.vendor_user_id("mockhub", "at")
    assert seen and set(seen) == {4}


def test_jwks_client_uses_the_configured_timeout():
    from token_broker.hub import HubValidator
    assert HubValidator(make_config(jwks_timeout_s=2))._jwks.timeout == 2


# ------------------------------------------------------------ registry ids


@pytest.mark.parametrize("vendor_id", ["../etc", "a/b", "Upper", "with space", ""])
def test_registry_ids_that_are_unsafe_path_segments_are_rejected(tmp_path, vendor_id):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({vendor_id: {"vendor_id": "x"}}))
    with pytest.raises(ConfigError, match="registry vendor ids"):
        VendorClient(make_config(registry_path=registry), MemoryCustody())


# ------------------------------------------------------------ revocation shapes


def _revocation_server(monkeypatch, answers: dict[str, int] | None = None) -> list[dict]:
    """A vendor with an RFC 7009 endpoint; `answers` maps token_type_hint to
    the status it returns (default 200). Returns the posted forms."""
    posted = []
    real = httpx.AsyncClient
    meta = {"issuer": "x", "authorization_endpoint": "http://as/a",
            "token_endpoint": "http://as/t", "revocation_endpoint": "http://as/r"}

    def handler(request):
        if request.url.path == "/r":
            form = dict(httpx.QueryParams(request.content.decode()))
            posted.append(form)
            return httpx.Response((answers or {}).get(form["token_type_hint"], 200), json={})
        return httpx.Response(200, json=meta)

    monkeypatch.setattr(vendors_mod.httpx, "AsyncClient",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    return posted


def _vendor_client() -> VendorClient:
    custody = MemoryCustody()
    custody.clients["mockhub"] = {"client_id": "cid", "client_secret": "s"}
    return VendorClient(make_config(), custody)


@pytest.mark.parametrize("entry,expected", [
    ({"refresh_token": "rt", "access_token": "at"},
     [("at", "access_token"), ("rt", "refresh_token")]),
    ({"refresh_token": "", "access_token": "at-forever"}, [("at-forever", "access_token")]),
])
async def test_rfc7009_revokes_the_access_token_then_the_refresh_token(monkeypatch, entry,
                                                                        expected):
    """Not every vendor kills the access tokens with the refresh token
    (Linear's live on for 24 h), so both are revoked, access token first.
    A non-expiring grant has no refresh token: never send an empty token."""
    posted = _revocation_server(monkeypatch)
    await _vendor_client().revoke("mockhub", entry)
    assert [(f["token"], f["token_type_hint"]) for f in posted] == expected


async def test_a_refused_access_token_revocation_is_not_a_failure(monkeypatch):
    """RFC 7009 lets a server refuse access-token revocation
    (unsupported_token_type): the refresh token's answer decides."""
    posted = _revocation_server(monkeypatch, {"access_token": 400})
    await _vendor_client().revoke("mockhub", {"refresh_token": "rt", "access_token": "at"})
    assert [f["token_type_hint"] for f in posted] == ["access_token", "refresh_token"]


@pytest.mark.parametrize("answers", [{"refresh_token": 400}, {"access_token": 503},
                                     {"refresh_token": 503}])
async def test_revocation_failures_leave_the_grant_pending(monkeypatch, answers):
    _revocation_server(monkeypatch, answers)
    with pytest.raises(vendors_mod.VendorUnavailable):
        await _vendor_client().revoke("mockhub", {"refresh_token": "rt", "access_token": "at"})
