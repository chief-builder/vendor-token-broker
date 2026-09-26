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

**The key constraint:** the lock is an **optimization**. It protects rotating refresh-token families from being burned. The **correctness guarantee** is the KV-v2 generation compare-and-swap (CAS): a write succeeds only if the stored generation has not changed. So the coordination backend needs speed and TTL (auto-expiry) support, not durability.

## Decision

Use Redis 7 behind a `Coordination` protocol (`coordination.py`). `COORD_BACKEND` selects the backend. `memory` stays the default. It keeps the lab's exact single-replica behavior and needs no extra infrastructure.

How each job maps to Redis (defaults updated for 1.1.0):

- **Lock:** `SET vtb:lock:{vendor}:{sub} <token> NX PX 20000`.
  - Waiters retry every 200ms±jitter, for up to 10s.
  - On each retry they re-read the entry. Once the generation moves forward, they serve it without taking the lock.
  - Release uses a compare-and-DEL Lua script, so only the holder ever releases a lock.
- **Consent transactions and states:** `SETEX` (TTL 600), consumed once with atomic `GETDEL`.
  - For states, the order is peek, check, then consume. So a rejection for an issuer (iss) mismatch does not burn the state the real callback needs.
- **Persisted `REFRESHING`:** lives in the storage (custody) entry, not in redis. The broker writes it with CAS before calling the vendor. The next lock holder takes over markers older than `REFRESHING_TTL_S`.
- **Mass-STALE window:** a per-vendor ZSET, plus `SET NX PX` to send each page only once.
- **Sweeper:** leader lease `SET NX PX 2×interval`, with ±20% jitter on the interval.
  - A stopped leader may keep its lease until it expires (120 seconds by default).
  - Successors wait for that lease before taking over.
- **Cache invalidation:** best-effort pub/sub on revoke, STALE, and delete. Without it, the worst case is still the per-replica `CACHE_TTL_S` (default 60s).

**If Redis is lost, the broker fails closed** with 503 `coordination-unavailable` on every path that needs redis:

- refresh,
- minting consent links (so resolves for absent, STALE, or insufficient-scope entries get 503, not 404/409),
- authorize,
- callbacks,
- DELETE.

Resolves that need no refresh keep serving. This mirrors the storage fail-closed behavior, with a problem title you can tell apart.

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
