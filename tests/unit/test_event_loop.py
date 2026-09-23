"""Blocking I/O never runs on the event loop (review H3).

Uses a real VaultStore whose blocking hvac layer is replaced by time.sleep
fakes, and a JWKS client that blocks, then measures what else the loop can
do meanwhile: tick a timer, serve a cache hit, answer /healthz. Also covers
the sweeper's per-pass budget."""
import asyncio
import time

from broker_harness import VENDOR, Harness, make_entry
from unit_helpers import StaticJWKS

from token_broker import sweeper
from token_broker.custody import VaultStore
from token_broker.hub import HubValidator


class BlockingVault(VaultStore):
    """VaultStore with the blocking layer faked: each call sleeps (like a
    slow hvac round-trip) inside whatever thread runs it."""

    def __init__(self, cfg, delay=0.005, slow_subs=(), slow_delay=1.0, list_delay=None):
        super().__init__(cfg)
        self.entries: dict[tuple[str, str], tuple[dict, int]] = {}
        self.delay, self.slow_subs, self.slow_delay = delay, set(slow_subs), slow_delay
        self.list_delay = delay if list_delay is None else list_delay

    def _sleep(self, sub=None):
        time.sleep(self.slow_delay if sub in self.slow_subs else self.delay)

    def _read(self, vendor, sub):
        self._sleep(sub)
        return self.entries.get((vendor, sub))

    def _list_subjects(self, vendor):
        time.sleep(self.list_delay)
        return [s for (v, s) in self.entries if v == vendor]


async def _max_stall(coro, tick=0.005) -> tuple[float, object]:
    """Run `coro` while a ticker measures the longest gap between ticks."""
    gaps, done = [], asyncio.Event()
    started = time.monotonic()   # before `coro` runs: a block before the first
                                 # tick must count as a stall too

    async def ticker():
        last = started
        while not done.is_set():
            await asyncio.sleep(tick)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    t = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        done.set()
        await t
    return max(gaps), result


async def test_sweep_over_slow_custody_does_not_stall_the_loop():
    h = Harness()
    vault = BlockingVault(h.cfg)
    for i in range(200):   # far from expiry: every entry is read, none refreshed
        vault.entries[(VENDOR, f"u{i}")] = (make_entry(expires_at=time.time() + 7200), 1)
    h.broker.custody = vault
    stall, _ = await _max_stall(sweeper.sweep_once(h.broker))
    # 200 blocking reads take ≥ 1s in total; on the loop they would stall it
    # for all of that. Off the loop, the ticker never waits more than a beat.
    assert stall < 0.2, f"event loop stalled {stall * 1000:.0f} ms"


async def test_slow_custody_listing_does_not_stall_the_loop():
    h = Harness()
    vault = BlockingVault(h.cfg, delay=0, list_delay=0.5)   # a large KV list call
    vault.entries[(VENDOR, "u1")] = (make_entry(expires_at=time.time() + 7200), 1)
    h.broker.custody = vault
    stall, _ = await _max_stall(sweeper.sweep_once(h.broker))
    assert stall < 0.2, f"event loop stalled {stall * 1000:.0f} ms"


async def test_cache_hit_serves_while_a_custody_read_hangs():
    """Review H3: during a custody stall, a cached token must still serve
    immediately (the documented ≤60s grace), not queue behind the stall."""
    h = Harness()
    vault = BlockingVault(h.cfg, delay=0, slow_subs={"slow"}, slow_delay=1.0)
    vault.entries[(VENDOR, "fast")] = (make_entry(expires_at=time.time() + 3600), 1)
    vault.entries[(VENDOR, "slow")] = (make_entry(expires_at=time.time() + 3600), 1)
    h.broker.custody = vault
    assert (await h.resolve(as_sub="fast")).status_code == 200      # warms the cache

    slow = asyncio.create_task(h.resolve(as_sub="slow"))            # hangs 1s in custody
    await asyncio.sleep(0.05)
    started = time.monotonic()
    r = await h.resolve(as_sub="fast")
    fast_latency = time.monotonic() - started
    assert r.status_code == 200
    assert fast_latency < 0.3, f"cache hit waited {fast_latency:.2f}s behind custody"
    assert (await slow).status_code == 200


async def test_blocking_jwks_fetch_does_not_stall_the_loop():
    class SlowJWKS(StaticJWKS):
        def get_signing_key_from_jwt(self, token):
            time.sleep(1.0)            # a JWKS refetch against a slow hub
            return super().get_signing_key_from_jwt(token)

    h = Harness()
    from broker_harness import hub_key
    h.broker.hub = HubValidator(h.cfg, jwks_client=SlowJWKS(hub_key().public_key()))
    h.put()
    stall, r = await _max_stall(h.resolve())
    assert r.status_code == 200
    assert stall < 0.3, f"event loop stalled {stall:.2f}s behind a JWKS fetch"


async def test_sweep_budget_covers_everything_over_consecutive_passes():
    h = Harness(sweep_max_entries=2)
    for i in range(5):     # all in the proactive band
        h.put(sub=f"u{i}", expires_at=time.time() + 600)
    calls = []
    for _ in range(3):
        await sweeper.sweep_once(h.broker)
        calls.append(h.vendors.refresh_calls)
    assert calls == [2, 4, 5]
    assert all(h.stored(f"u{i}")["refresh_generation"] == 2 for i in range(5))
