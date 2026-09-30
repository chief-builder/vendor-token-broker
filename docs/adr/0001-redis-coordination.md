# ADR-0001: Redis 7 as the multi-replica coordination backend

**Status:** accepted · **Date:** 2026-07-16

This record explains why the broker uses Redis 7 to coordinate more than one replica.

## Context

The source lab ran the broker as one replica. There:

- An in-process asyncio lock per `{vendor, sub}` made sure only one refresh ran at a time (single-flight refresh).
- Consent transactions and `state` records lived in in-process dicts.
- The sweeper always ran.

This was documented as correct **only** for one replica. Running more than one replica needs all of these together:

- a single-flight lock shared across replicas,
- a persisted `REFRESHING` marker,
- a sweeper with jitter, run only by an elected leader,
- shared, single-use consent state (or session affinity at the load balancer),
- honest docs about the token cache being per replica.

**The key constraint:** the lock is an **optimization**. It protects rotating refresh-token families from being burned. The **correctness guarantee** is the KV-v2 compare-and-swap (CAS): a write succeeds only if the entry's KV version has not changed since it was read. The entry's `refresh_generation` only goes up, so a CAS loser can never write an older pair. So the coordination backend needs speed and TTL (auto-expiry) support, not durability.

## Decision

Use Redis 7 behind a `Coordination` protocol (`coordination.py`). `COORD_BACKEND` selects the backend. `memory` stays the default. It keeps the lab's exact single-replica behavior and needs no extra infrastructure.

How each job maps to Redis (with the default settings):

- **Lock:** `SET vtb:lock:{vendor}:{sub} <token> NX PX 20000`.
  - The PX TTL is `LOCK_TTL_MS` (default 20000). Startup fails if it is shorter than `(VENDOR_TIMEOUT_S + 2 × VAULT_TIMEOUT_S) × 1000`, so the lock outlives the slowest refresh it protects.
  - Waiters retry every 200ms ± 25%, for up to `LOCK_TIMEOUT_S` (10s).
  - On each retry, a waiting resolve re-reads the entry. Once the generation moves forward, it serves that token without taking the lock.
  - Release uses a compare-and-DEL Lua script, so only the holder ever releases a lock. If the release fails, the PX TTL frees the lock.
  - The sweeper takes the lock without waiting and skips a held entry.
  - DELETE and the consent write also wait for this lock. The sweeper's revoke retry takes it without waiting.
- **Consent transactions and states:** `SETEX` (`TXN_TTL_S`, 600), consumed once with atomic `GETDEL`. An authorize link must be used within 300 s (`AUTHORIZE_LINK_TTL_S` in `consent.py`).
  - For states, the order is peek, check, then consume. So a rejection for an issuer (iss) mismatch does not burn the state the real callback needs.
- **Persisted `REFRESHING`:** lives in the storage (custody) entry, not in redis, with `refresh_owner` and `refresh_started_at`. The broker writes it with CAS before calling the vendor. If the vendor is unreachable or errors, the broker CAS-writes the entry back to ACTIVE at once. The next lock holder takes over markers older than `REFRESHING_TTL_S` (30s), and never refreshes over a younger one.
- **Mass-STALE window:** a per-vendor ZSET, plus `SET NX PX` to send each page only once.
- **Sweeper:** leader lease `SET vtb:sweep-lease <instance> NX PX 2×interval`, with ±20% jitter on the interval. The holder renews it with a compare-and-PEXPIRE Lua script, so it never extends a lease another replica took over.
  - A stopped leader may keep its lease until it expires (120 seconds by default).
  - Successors wait for that lease before taking over.
- **Cache invalidation:** best-effort pub/sub on channel `vtb:invalidate` when an entry goes STALE, is parked REVOKE_PENDING, is deleted, or is replaced by a completed consent. The listener re-subscribes after a dropped connection. Without it, the worst case is still the per-replica `CACHE_TTL_S` (default 60s).

**If Redis is lost, the broker fails closed** on every path that needs redis. These answer with problem JSON 503 `coordination-unavailable`:

- resolves that need a refresh,
- resolves that need a consent link (so absent, STALE, or insufficient-scope entries get 503, not 404/409),
- authorize,
- DELETE,
- the hub callback when it starts the vendor step.

The browser callbacks (`/v1/callback/_hub` and `/v1/callback/{vendor}`), when they cannot read or use up a state, answer with the HTML page "Coordination store unavailable." and status 503. That page carries no problem `title`. The sweeper skips its tick.

Resolves that need no refresh keep serving. This mirrors the storage fail-closed behavior, with a problem title you can tell apart. The mass-STALE window is not counted while Redis is down, but each `broker.stale` event is still written.

## Alternatives considered

- **Postgres advisory locks:** support fencing and are durable. But they add a relational database to a service with no relational state. TTL and pub/sub would have to be emulated.
- **DynamoDB conditional writes:** workable for AWS-only estates. Higher latency on the lock hot path. No pub/sub.
- **Storage backend only (CAS, no lock):** correct, but burns rotating refresh-token families under concurrency. That is exactly the kind of incident the broker exists to prevent.

## Consequences

- Multi-replica deployments gain one infrastructure dependency. Single-replica deployments gain none (`memory` default).
- Consent flows need no session affinity at the load balancer.
- All coordination state can be rebuilt. You can flush redis with no effect on stored tokens: in-flight consents restart and locks form again.
- `tests/integration/test_multi_replica.py` proves:
  - exactly one refresh across replicas, with zero refresh-token replays,
  - consent that crosses replicas,
  - a single sweep leader,
  - takeover of abandoned markers,
  - redis-down behavior,
  - cache invalidation.
