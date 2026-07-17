# ADR-0001: Redis 7 as the multi-replica coordination backend

**Status:** accepted · **Date:** 2026-07-16

## Context

The source lab ran the broker single-replica: an in-process asyncio lock
per `{vendor, sub}` gave single-flight refresh, consent transactions and
`state` records lived in process dicts, and the sweeper ran unconditionally.
Documented as correct **only** for one replica. Anything beyond one replica
needs, together (blueprint §3): a distributed single-flight lock, persisted
`REFRESHING`, a jittered leader-elected sweeper, shared single-use consent
state (or session affinity), and honesty about the per-replica token cache.

The crucial constraint: the lock is an **optimization** that protects
rotating refresh-token families from being burned; the KV-v2 generation
compare-and-swap is the **correctness guarantee**. The coordination backend
therefore needs speed and TTL semantics, not durability.

## Decision

Redis 7 behind a `Coordination` protocol (`coordination.py`), selected by
`COORD_BACKEND`; `memory` stays the default and preserves the lab's exact
single-replica semantics with zero extra infrastructure.

Redis mapping:
- Lock: `SET vtb:lock:{vendor}:{sub} <token> NX PX 15000`; waiters retry
  200ms±jitter up to 10s, re-reading the entry each retry and serving
  without the lock once the generation advances; release via
  compare-and-DEL Lua (a lock is only ever released by its holder).
- Consent txns/states: `SETEX` (TTL 600) with atomic `GETDEL` single-use
  consumption; peek-validate-consume ordering keeps an iss-mismatch
  rejection from burning the state the legitimate callback needs.
- Persisted `REFRESHING` lives in the custody entry (not redis), CAS-written
  before the vendor call; abandoned markers (> `REFRESHING_TTL_S`) are taken
  over by the next lock holder.
- Mass-STALE window: per-vendor ZSET + `SET NX PX` page dedup.
- Sweeper: leader lease `SET NX PX 2×interval`, ±20% interval jitter.
- Cache invalidation: best-effort pub/sub on revoke/STALE/delete; worst
  case without it stays the documented ≤60s per-replica cache TTL.

Redis loss fails the refresh path closed (503 `coordination-unavailable`)
while cache hits keep serving — mirroring the custody fail-closed posture
with a distinguishable problem title.

## Alternatives considered

- **Postgres advisory locks**: fencing-capable and durable, but drags a
  relational database into a service that needs no relational state; TTL
  and pub/sub must be emulated.
- **DynamoDB conditional writes**: viable for AWS-only estates; higher
  latency on the lock hot path; no pub/sub.
- **Custody-backend-only (CAS, no lock)**: correct but burns rotating RT
  families under concurrency — exactly the incident class the broker
  exists to prevent.

## Consequences

- Multi-replica deployments gain one infrastructure dependency; single
  replica deployments gain none (`memory` default).
- Consent flows need no session affinity at the balancer.
- All coordination state is reconstructible: redis can be flushed with no
  custody impact (in-flight consents restart; locks re-form).
- Proven by `tests/integration/test_multi_replica.py`: exactly-one refresh
  across replicas with zero RT replays, cross-replica consent, single sweep
  leader, abandoned-marker takeover, redis-down semantics, cache
  invalidation.
