"""Coordination backends: single-flight refresh locks, consent transaction
and state stores, the mass-STALE window, sweeper leadership, and best-effort
cache invalidation.

`memory` keeps the single-replica semantics of the source lab exactly: an
in-process asyncio lock per {vendor, sub}, dict-backed consent stores with
TTL checks at read, no lease (always the sweep leader), no jitter, no
broadcast. Correct ONLY while replicas = 1.

`redis` is the multi-replica profile (ADR-0001): SET NX PX lock with
compare-and-DEL release, waiters retrying 200ms±jitter re-reading the entry
each retry; SETEX consent records with atomic GETDEL single-use consumption;
per-vendor ZSET mass-STALE window; sweep leader lease; pub/sub cache
invalidation. Redis loss fails the refresh path closed (503
coordination-unavailable) — cache hits still serve, and the KV-v2 CAS
remains the correctness backstop regardless of lock behavior.
"""

import asyncio
import json
import logging
import random
import secrets
import time
from collections.abc import Callable
from typing import Any, Protocol

from .config import Config

log = logging.getLogger(__name__)
CLEANUP_INTERVAL_S = 60  # memory backend: expired-record reclamation cadence
LOCK_RETRY_S = 0.2  # waiter poll interval (±25% jitter), ADR-0001


class CoordinationUnavailable(Exception):
    """Redis unreachable. Fixed message: raw errors carry host:port and go to
    the debug log only."""

    def __init__(self, cause: Exception | None = None):
        super().__init__("coordination store unavailable")
        if cause is not None:
            log.debug("coordination error: %r", cause)


class Coordination(Protocol):
    persist_refreshing: bool

    async def start(self, on_invalidate: Callable[[str, str], None]) -> None: ...
    async def close(self) -> None: ...

    async def wait_refresh_lock(
        self, vendor: str, sub: str, should_stop
    ) -> tuple[str | None, Any]: ...
    async def try_refresh_lock(self, vendor: str, sub: str) -> str | None: ...
    async def release_refresh_lock(self, vendor: str, sub: str, token: str) -> None: ...

    async def put_txn(self, txn_id: str, record: dict) -> None: ...
    # Read without consuming. Not used by the routes (they take_txn); tests
    # inspect consent transactions through it.
    async def get_txn(self, txn_id: str) -> dict | None: ...
    async def take_txn(self, txn_id: str) -> dict | None: ...

    async def put_state(self, state: str, record: dict) -> None: ...
    async def peek_state(self, state: str) -> dict | None: ...
    async def consume_state(self, state: str) -> dict | None: ...

    async def record_stale(self, vendor: str) -> tuple[int, bool]: ...

    async def acquire_sweep_lease(self) -> bool: ...
    def sweep_jitter(self) -> float: ...
    async def cleanup(self) -> None: ...

    async def publish_invalidate(self, vendor: str, sub: str) -> None: ...


class MemoryCoordination:
    """The lab's exact single-replica semantics behind the protocol."""

    persist_refreshing = False

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._lock_users: dict[tuple[str, str], int] = {}
        self._txns: dict[str, dict] = {}
        self._states: dict[str, dict] = {}
        self._stale_events: dict[str, list[float]] = {}
        self._paged_vendors: dict[str, float] = {}
        self._cleanup_task: asyncio.Task | None = None

    async def start(self, on_invalidate) -> None:
        # Reclaim expired consent records on a timer of their own: the
        # sweeper can be disabled (SWEEP_INTERVAL_S=0), and records must not
        # grow without bound when it is (review M10).
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def close(self) -> None:
        await _stop(self._cleanup_task)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_S)
            await self.cleanup()

    # Per-entry locks are reference-counted and dropped when idle, so the
    # table holds only entries with a refresh in flight or waiters.

    def _use_lock(self, key: tuple[str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        return lock

    def _unuse_lock(self, key: tuple[str, str]) -> None:
        self._lock_users[key] -= 1
        if self._lock_users[key] == 0:
            del self._lock_users[key]
            del self._locks[key]

    async def wait_refresh_lock(self, vendor, sub, should_stop) -> tuple[str | None, Any]:
        # Park on the lock exactly like the lab: no mid-wait re-reads; the
        # caller re-reads once after acquisition (or after timeout).
        key = (vendor, sub)
        lock = self._use_lock(key)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=self.cfg.lock_timeout_s)
            return "local", None
        except TimeoutError:
            self._unuse_lock(key)
            return None, None
        except BaseException:
            self._unuse_lock(key)
            raise

    async def try_refresh_lock(self, vendor, sub) -> str | None:
        lock = self._locks.get((vendor, sub))
        if lock is not None and lock.locked():
            return None
        await self._use_lock((vendor, sub)).acquire()  # free: returns at once
        return "local"

    async def release_refresh_lock(self, vendor, sub, token) -> None:
        key = (vendor, sub)
        self._locks[key].release()
        self._unuse_lock(key)

    def _fresh(self, record: dict | None) -> dict | None:
        if record is None or time.time() - record["created_at"] > self.cfg.txn_ttl_s:
            return None
        return record

    async def put_txn(self, txn_id, record) -> None:
        self._txns[txn_id] = record

    async def get_txn(self, txn_id) -> dict | None:
        return self._fresh(self._txns.get(txn_id))

    async def take_txn(self, txn_id) -> dict | None:
        return self._fresh(self._txns.pop(txn_id, None))

    async def put_state(self, state, record) -> None:
        self._states[state] = {**record, "consumed": False}

    async def peek_state(self, state) -> dict | None:
        record = self._fresh(self._states.get(state))
        if record is None or record["consumed"]:
            return None
        return record

    async def consume_state(self, state) -> dict | None:
        record = self._fresh(self._states.get(state))
        if record is None or record["consumed"]:
            return None
        record["consumed"] = True  # kept until swept; replays see 'consumed'
        return record

    async def record_stale(self, vendor) -> tuple[int, bool]:
        now = time.time()
        window = self.cfg.mass_stale_window_s
        events = [t for t in self._stale_events.get(vendor, []) if now - t < window]
        events.append(now)
        self._stale_events[vendor] = events
        page = (
            len(events) >= self.cfg.mass_stale_threshold
            and now - self._paged_vendors.get(vendor, 0) > window
        )
        if page:
            self._paged_vendors[vendor] = now
        return len(events), page

    async def acquire_sweep_lease(self) -> bool:
        return True  # single replica: always the leader

    def sweep_jitter(self) -> float:
        return 0.0  # fixed interval, as in the lab

    async def cleanup(self) -> None:
        now = time.time()
        for store in (self._txns, self._states):
            for key in [k for k, v in store.items() if now - v["created_at"] > self.cfg.txn_ttl_s]:
                store.pop(key, None)
        for vendor in list(self._stale_events):
            kept = [t for t in self._stale_events[vendor] if now - t < self.cfg.mass_stale_window_s]
            if kept:
                self._stale_events[vendor] = kept
            else:
                self._stale_events.pop(vendor, None)
        for vendor in [
            v for v, t in self._paged_vendors.items() if now - t > self.cfg.mass_stale_window_s
        ]:
            del self._paged_vendors[vendor]

    async def publish_invalidate(self, vendor, sub) -> None:
        pass  # single replica: the local drop already happened


_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end
"""

# Lease renewal: extend only while we still hold it (atomic; GET-then-PEXPIRE
# could extend a lease another replica took over in between).
_RENEW_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("pexpire", KEYS[1], ARGV[2])
else
  return 0
end
"""

_CHANNEL = "vtb:invalidate"


async def _stop(task: asyncio.Task | None) -> None:
    """Cancel a background task and wait for it to finish."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class RedisCoordination:
    """Multi-replica profile on Redis 7.4 or later; tested on 8.10 (ADR-0001)."""

    persist_refreshing = True

    def __init__(self, cfg: Config, instance_id: str, client=None):
        import redis.asyncio as aioredis
        from redis import exceptions as redis_exc

        self.cfg = cfg
        self.instance_id = instance_id
        self._exc = redis_exc.RedisError
        self._r = (
            client
            if client is not None
            else aioredis.from_url(
                cfg.redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=3
            )
        )
        self._release = self._r.register_script(_RELEASE_LUA)
        self._renew = self._r.register_script(_RENEW_LUA)
        self._on_invalidate = None
        self._listen_task: asyncio.Task | None = None

    async def start(self, on_invalidate) -> None:
        self._on_invalidate = on_invalidate
        self._listen_task = asyncio.create_task(self._listen())

    async def close(self) -> None:
        await _stop(self._listen_task)
        await self._r.aclose()

    async def _listen(self) -> None:
        """Best-effort invalidation listener; a dropped subscription is retried
        forever — worst case stays the documented ≤60s cache TTL."""
        while True:
            pubsub = self._r.pubsub()
            try:
                await pubsub.subscribe(_CHANNEL)
                async for msg in pubsub.listen():
                    if msg["type"] == "message":
                        vendor, _, sub = str(msg["data"]).partition("|")
                        if self._on_invalidate is not None:
                            self._on_invalidate(vendor, sub)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)
            finally:
                try:  # release the dead subscription's connection
                    await pubsub.aclose()
                except Exception:
                    pass

    @staticmethod
    def _lock_key(vendor: str, sub: str) -> str:
        return f"vtb:lock:{vendor}:{sub}"

    async def wait_refresh_lock(self, vendor, sub, should_stop) -> tuple[str | None, Any]:
        token = secrets.token_hex(16)
        deadline = time.monotonic() + self.cfg.lock_timeout_s
        key = self._lock_key(vendor, sub)
        while True:
            try:
                acquired = await self._r.set(key, token, nx=True, px=self.cfg.lock_ttl_ms)
            except self._exc as exc:
                raise CoordinationUnavailable(exc) from exc
            if acquired:
                return token, None
            if should_stop is not None:
                early = await should_stop()
                if early is not None:
                    return None, early
            if time.monotonic() >= deadline:
                return None, None
            await asyncio.sleep(LOCK_RETRY_S * random.uniform(0.75, 1.25))

    async def try_refresh_lock(self, vendor, sub) -> str | None:
        token = secrets.token_hex(16)
        try:
            acquired = await self._r.set(
                self._lock_key(vendor, sub), token, nx=True, px=self.cfg.lock_ttl_ms
            )
        except self._exc as exc:
            raise CoordinationUnavailable(exc) from exc
        return token if acquired else None

    async def release_refresh_lock(self, vendor, sub, token) -> None:
        try:
            await self._release(keys=[self._lock_key(vendor, sub)], args=[token])
        except self._exc:
            pass  # PX TTL is the fallback release

    async def _setex_json(self, key: str, record: dict) -> None:
        try:
            await self._r.set(key, json.dumps(record), ex=self.cfg.txn_ttl_s)
        except self._exc as exc:
            raise CoordinationUnavailable(exc) from exc

    async def _get_json(self, key: str) -> dict | None:
        try:
            raw = await self._r.get(key)
        except self._exc as exc:
            raise CoordinationUnavailable(exc) from exc
        return json.loads(raw) if raw else None

    async def put_txn(self, txn_id, record) -> None:
        await self._setex_json(f"vtb:txn:{txn_id}", record)

    async def get_txn(self, txn_id) -> dict | None:
        return await self._get_json(f"vtb:txn:{txn_id}")

    async def take_txn(self, txn_id) -> dict | None:
        """Atomic single-use take: GETDEL — one link starts one flow."""
        try:
            raw = await self._r.getdel(f"vtb:txn:{txn_id}")
        except self._exc as exc:
            raise CoordinationUnavailable(exc) from exc
        return json.loads(raw) if raw else None

    async def put_state(self, state, record) -> None:
        await self._setex_json(f"vtb:state:{state}", record)

    async def peek_state(self, state) -> dict | None:
        return await self._get_json(f"vtb:state:{state}")

    async def consume_state(self, state) -> dict | None:
        """Atomic single-use take: GETDEL — exactly one consumer wins."""
        try:
            raw = await self._r.getdel(f"vtb:state:{state}")
        except self._exc as exc:
            raise CoordinationUnavailable(exc) from exc
        return json.loads(raw) if raw else None

    async def record_stale(self, vendor) -> tuple[int, bool]:
        now = time.time()
        window = self.cfg.mass_stale_window_s
        zkey = f"vtb:stale:{vendor}"
        try:
            pipe = self._r.pipeline()
            pipe.zremrangebyscore(zkey, 0, now - window)
            pipe.zadd(zkey, {f"{now}:{secrets.token_hex(4)}": now})
            pipe.zcard(zkey)
            pipe.expire(zkey, window * 2)
            results = await pipe.execute()
            count = int(results[2])
            page = False
            if count >= self.cfg.mass_stale_threshold:
                page = bool(
                    await self._r.set(f"vtb:paged:{vendor}", "1", nx=True, px=window * 1000)
                )
            return count, page
        except self._exc as exc:
            raise CoordinationUnavailable(exc) from exc

    async def acquire_sweep_lease(self) -> bool:
        """Leader lease: SET NX PX 2×interval; the holder renews, others skip."""
        lease_ms = max(1000, 2 * self.cfg.sweep_interval_s * 1000)
        try:
            if await self._r.set("vtb:sweep-lease", self.instance_id, nx=True, px=lease_ms):
                return True
            renewed = await self._renew(keys=["vtb:sweep-lease"], args=[self.instance_id, lease_ms])
            return bool(renewed)
        except self._exc as exc:
            raise CoordinationUnavailable(exc) from exc

    def sweep_jitter(self) -> float:
        return random.uniform(-0.2, 0.2)  # ±20% (ADR-0001)

    async def cleanup(self) -> None:
        pass  # SETEX/EXPIRE handle expiry server-side

    async def publish_invalidate(self, vendor, sub) -> None:
        try:
            await self._r.publish(_CHANNEL, f"{vendor}|{sub}")
        except self._exc as exc:
            raise CoordinationUnavailable(exc) from exc


def make_coordination(cfg: Config, instance_id: str) -> Coordination:
    if cfg.coord_backend == "redis":
        return RedisCoordination(cfg, instance_id)
    return MemoryCoordination(cfg)
