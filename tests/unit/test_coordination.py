"""Coordination backends: memory (lab-exact single-replica semantics) and
redis (fakeredis) — locks, single-use consent state, mass-STALE window,
sweep lease, and invalidation publishing."""
import asyncio
import time

import pytest
from unit_helpers import make_config

from token_broker.coordination import MemoryCoordination, RedisCoordination


@pytest.fixture
def mem():
    return MemoryCoordination(make_config(lock_timeout_s=1))


@pytest.fixture
def red():
    import fakeredis.aioredis
    cfg = make_config(lock_timeout_s=1, lock_ttl_ms=15000)
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RedisCoordination(cfg, instance_id="inst-a", client=client)


@pytest.fixture
def red_b(red):
    """A second replica sharing the first's fakeredis server."""
    cfg = make_config(lock_timeout_s=1, lock_ttl_ms=15000)
    return RedisCoordination(cfg, instance_id="inst-b", client=red._r)


# ---------------------------------------------------------------- locks

async def test_memory_lock_single_flight(mem):
    t1, _ = await mem.wait_refresh_lock("v", "s", None)
    assert t1 is not None
    t2, early = await mem.wait_refresh_lock("v", "s", None)  # times out (1s)
    assert t2 is None and early is None
    await mem.release_refresh_lock("v", "s", t1)
    t3, _ = await mem.wait_refresh_lock("v", "s", None)
    assert t3 is not None


async def test_memory_try_lock_nonblocking(mem):
    t1 = await mem.try_refresh_lock("v", "s")
    assert t1 is not None
    assert await mem.try_refresh_lock("v", "s") is None
    await mem.release_refresh_lock("v", "s", t1)


async def test_redis_lock_single_flight_and_release(red, red_b):
    t1, _ = await red.wait_refresh_lock("v", "s", None)
    assert t1 is not None
    assert await red_b.try_refresh_lock("v", "s") is None
    # Wrong token cannot release (compare-and-DEL).
    await red_b.release_refresh_lock("v", "s", "not-the-token")
    assert await red_b.try_refresh_lock("v", "s") is None
    await red.release_refresh_lock("v", "s", t1)
    t2 = await red_b.try_refresh_lock("v", "s")
    assert t2 is not None


async def test_redis_waiter_serves_early_when_gen_advances(red, red_b):
    t1, _ = await red.wait_refresh_lock("v", "s", None)
    calls = 0

    async def should_stop():
        nonlocal calls
        calls += 1
        return "the-response" if calls >= 2 else None

    token, early = await red_b.wait_refresh_lock("v", "s", should_stop)
    assert token is None and early == "the-response"
    assert calls == 2
    await red.release_refresh_lock("v", "s", t1)


async def test_redis_waiter_times_out(red, red_b):
    t1, _ = await red.wait_refresh_lock("v", "s", None)
    start = time.monotonic()
    token, early = await red_b.wait_refresh_lock("v", "s", None)
    assert token is None and early is None
    assert time.monotonic() - start >= 1.0  # cfg.lock_timeout_s
    await red.release_refresh_lock("v", "s", t1)


# ------------------------------------------------- consent txns and state

@pytest.mark.parametrize("backend", ["mem", "red"])
async def test_txn_roundtrip(backend, request):
    coord = request.getfixturevalue(backend)
    await coord.put_txn("t1", {"sub": "alice", "vendor": "v",
                               "scopes": ["a"], "created_at": time.time()})
    rec = await coord.get_txn("t1")
    assert rec["sub"] == "alice"
    await coord.pop_txn("t1")
    assert await coord.get_txn("t1") is None


async def test_memory_txn_ttl_expires(mem):
    await mem.put_txn("old", {"sub": "a", "vendor": "v", "scopes": [],
                              "created_at": time.time() - 601})
    assert await mem.get_txn("old") is None


@pytest.mark.parametrize("backend", ["mem", "red"])
async def test_state_is_single_use(backend, request):
    coord = request.getfixturevalue(backend)
    await coord.put_state("s1", {"txn_id": "t", "sub": "a", "vendor": "v",
                                 "pkce_verifier": "pv", "issuer": "i",
                                 "iss_required": True, "scopes": [],
                                 "created_at": time.time()})
    assert (await coord.peek_state("s1"))["pkce_verifier"] == "pv"
    # Peeking does not consume (an iss-mismatch rejection must not burn it).
    assert await coord.peek_state("s1") is not None
    first = await coord.consume_state("s1")
    assert first is not None
    assert await coord.consume_state("s1") is None  # replay loses
    assert await coord.peek_state("s1") is None


@pytest.mark.parametrize("backend", ["mem", "red"])
async def test_concurrent_consumption_has_one_winner(backend, request):
    coord = request.getfixturevalue(backend)
    await coord.put_state("race", {"txn_id": "t", "sub": "a", "vendor": "v",
                                   "pkce_verifier": "pv", "issuer": None,
                                   "iss_required": False, "scopes": [],
                                   "created_at": time.time()})
    results = await asyncio.gather(*[coord.consume_state("race") for _ in range(5)])
    assert sum(r is not None for r in results) == 1


# ------------------------------------------------------ mass-STALE window

@pytest.mark.parametrize("backend", ["mem", "red"])
async def test_mass_stale_pages_at_threshold_once(backend, request):
    coord = request.getfixturevalue(backend)
    count, page = await coord.record_stale("v")
    assert (count, page) == (1, False)
    count, page = await coord.record_stale("v")
    assert (count, page) == (2, False)
    count, page = await coord.record_stale("v")
    assert count == 3 and page is True          # threshold (3) hit -> page
    count, page = await coord.record_stale("v")
    assert count == 4 and page is False          # deduped within the window


@pytest.mark.parametrize("backend", ["mem", "red"])
async def test_mass_stale_window_is_per_vendor(backend, request):
    coord = request.getfixturevalue(backend)
    for _ in range(2):
        await coord.record_stale("v1")
    count, page = await coord.record_stale("v2")
    assert (count, page) == (1, False)


# ------------------------------------------------------------ sweep lease

async def test_memory_is_always_sweep_leader(mem):
    assert await mem.acquire_sweep_lease() is True
    assert mem.sweep_jitter() == 0.0


async def test_redis_sweep_lease_single_leader(red, red_b):
    assert await red.acquire_sweep_lease() is True
    assert await red_b.acquire_sweep_lease() is False  # lease held by inst-a
    assert await red.acquire_sweep_lease() is True     # holder renews
    assert -0.2 <= red.sweep_jitter() <= 0.2


# ------------------------------------------------------------ invalidation

async def test_redis_publish_invalidate_reaches_subscriber(red, red_b):
    seen = []
    await red_b.start(on_invalidate=lambda v, s: seen.append((v, s)))
    await asyncio.sleep(0.1)  # let the subscription attach
    await red.publish_invalidate("mockhub", "alice")
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.02)
    await red_b.close()
    assert seen == [("mockhub", "alice")]
