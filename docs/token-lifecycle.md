# Token lifecycle — sequence diagrams

Every path a vendor grant can take through the broker, from first consent
to deletion, illustrating the implementation and its failure paths (timings are the
defaults from `config.py`). Companion documents: `design.md` (normative
behavior), `smoke-tests.md` (hands-on verification of these same paths).

**Cast**, common to all diagrams:

| Actor | Role |
|---|---|
| Client | The trusted gateway calling the broker with the user's internal hub JWT; not the MCP client |
| Broker | This service (one replica unless the diagram says otherwise) |
| Vendor AS | The third-party authorization server (GitHub-class: rotating refresh tokens) |
| Custody | OpenBao/Vault KV-v2 — the entry's KV version is the CAS handle |
| Redis | Coordination backend (multi-replica profile only) |

## 0. Orientation — the states and which diagram covers each transition

```mermaid
stateDiagram-v2
    classDef live fill:#10b98122,stroke:#10b981
    classDef transient fill:#6366f122,stroke:#6366f1
    classDef dead fill:#ef444422,stroke:#ef4444

    [*] --> NoGrant
    NoGrant --> ACTIVE : §1 consent
    ACTIVE --> ACTIVE : §2–§6 refresh
    ACTIVE --> REFRESHING
    REFRESHING --> ACTIVE : §5 CAS ok
    REFRESHING --> STALE : §5 replay
    ACTIVE --> STALE : §7 invalid_grant
    STALE --> ACTIVE : §7 re-consent
    ACTIVE --> NoGrant : §8 revoke
    ACTIVE --> REVOKE_PENDING : §8 vendor down
    REVOKE_PENDING --> NoGrant : §8 sweep retry

    class ACTIVE live
    class REFRESHING transient
    class STALE,REVOKE_PENDING dead
```

| Transition group | Diagram | What happens |
|---|---|---|
| §1 | Birth | consent dance writes `ACTIVE gen=1` |
| §2–§4 | Steady state / refresh | cache-hit serve, lazy + proactive refresh (`gen+1`) |
| §5 | Multi-replica | persisted `REFRESHING` marker, takeover, CAS backstop |
| §6 | Scope step-up | re-consent union writes a fresh `gen=1` |
| §7 | STALE | `invalid_grant` → re-consent (mass burst pages) |
| §8 | Revocation | vendor-first delete, `REVOKE_PENDING` parking |

`REFRESHING` exists only in the redis profile and never surfaces
externally (`/v1/grants` reports it as `ACTIVE`).

---

## 1. Birth — first-time consent dance

Trigger: a resolve finds no entry (or a STALE one) for `{vendor, sub}`.

```mermaid
sequenceDiagram
    autonumber
    box rgba(148,163,184,0.14) User side
        participant C as Client
        participant UA as Browser
    end
    box rgba(99,102,241,0.20) Broker
        participant B as Broker
    end
    box rgba(148,163,184,0.14) Hub
        participant H as Hub (IdP)
    end
    box rgba(234,179,8,0.16) Vendor
        participant V as Vendor AS
    end
    box rgba(16,185,129,0.16) Custody
        participant K as Custody
    end

    C->>B: POST /v1/tokens/resolve (hub JWT)
    Note over B: hub JWT re-validated:<br/>PS256/ES256 pinned, issuer,<br/>exactly one tier audience, mcp_contract
    B->>K: read entry
    K-->>B: not found
    B-->>C: 404 needs-consent + authorize_uri(txn)
    C->>UA: open authorize_uri
    UA->>B: GET /v1/authorize/{vendor}?txn=…
    Note over B: link used up (single use, ≤ 5 min old)<br/>set binding cookie (HttpOnly, SameSite=Lax)
    B-->>UA: 307 → hub login (PKCE, nonce, login_hint=sub)
    UA->>H: user signs in
    H-->>UA: 302 → /v1/callback/_hub?code&state
    UA->>B: GET /v1/callback/_hub (with binding cookie)
    B->>H: POST /token (code + PKCE verifier)
    H-->>B: ID token
    Note over B: ID token: signature, iss, aud, exp, nonce<br/>logged-in sub MUST equal the link's sub<br/>(else 403 + security event, no vendor leg)
    Note over B: mint vendor PKCE verifier (S256) +<br/>single-use state {sub, vendor, issuer,<br/>scopes ≤ registry ceiling, binding} — record<br/>whether the AS advertises RFC 9207 iss
    B-->>UA: 307 → vendor authorize<br/>(client_id, code_challenge, state)
    UA->>V: user consents as themself
    V-->>UA: 302 → /v1/callback/{vendor}?code&state&iss
    UA->>B: GET /v1/callback/{vendor}?code&state&iss
    Note over B: 1. state exists, unconsumed, vendor matches<br/>2. iss == recorded issuer (strict string —<br/>   omission = mix-up if advertised)<br/>3. binding cookie = the starting browser<br/>4. consume state — single use, BEFORE redeem
    B->>V: POST /token (code + PKCE verifier + client auth¹)
    V-->>B: access token + refresh token (rotating)
    B->>V: GET userinfo → vendor_user_id
    B->>K: write entry state=ACTIVE gen=1 (cas=None: re-consent overwrites)
    B-->>UA: "Connected — return to your client."
    C->>B: retry resolve
    B-->>C: 200 access_token
```

¹ Client auth per registry `token_endpoint_auth_method`:
`client_secret_post`, `client_secret_basic`, or `private_key_jwt`
(RFC 7523 assertion signed with the key from `vendor-clients/{vendor}`).

Defenses in this diagram: only the user the link was issued for, in the
browser that opened it, can complete consent (hub login + binding cookie);
the code is redeemed server-side only; the browser sees only opaque state handles; the verifier is sent only in server-side token requests; a replayed
callback (state already consumed) is a 400 **and** a
`security_event: true` audit line; a tampered or missing `iss` is rejected
*before* consumption, so the legitimate callback still completes.

---

## 2. Steady state — cache-hit resolve

Condition: `expires_at − now ≥ max(min_ttl_s, REFRESH_BUFFER_S=300)`.

```mermaid
sequenceDiagram
    autonumber
    box rgba(148,163,184,0.14) User side
        participant C as Client
    end
    box rgba(99,102,241,0.20) Broker
        participant B as Broker
    end
    box rgba(16,185,129,0.16) Custody
        participant K as Custody
    end

    C->>B: resolve (hub JWT)
    alt per-replica cache fresh (≤ CACHE_TTL_S, default 60s)
        Note over B: serve from memory — no custody read
    else cache miss/expired
        B->>K: read entry
        K-->>B: ACTIVE, expires_at, version
        Note over B: cache {entry, version} for ≤ CACHE_TTL_S
    end
    B-->>C: 200 {access_token, expires_at, granted_scopes}
```

This is the hot path (SLO p99 ≤ 25 ms). The 60-second cache is also the
**only** grace the broker allows during a custody outage (§9).

---

## 3. Lazy refresh — single-flight inside the buffer

Condition: entry ACTIVE but `expires_at − now < max(min_ttl, 300)`.
Rotating-RT vendors revoke the whole token family if a consumed refresh
token is replayed — so concurrent resolves must produce exactly one
vendor call.

```mermaid
sequenceDiagram
    autonumber
    box rgba(148,163,184,0.14) Callers
        participant C1 as Caller 1
        participant C2 as Caller 2 (concurrent)
    end
    box rgba(99,102,241,0.20) Broker
        participant B as Broker
    end
    box rgba(234,179,8,0.16) Vendor
        participant V as Vendor AS
    end
    box rgba(16,185,129,0.16) Custody
        participant K as Custody
    end

    par both inside the buffer
        C1->>B: resolve
    and
        C2->>B: resolve
    end
    Note over B: per-{vendor,sub} lock — C1 acquires,<br/>C2 parks (memory) / polls 200ms±jitter (redis)
    B->>K: re-read under lock (gen = N, version = v)
    B->>V: refresh_token grant (RT gen N)
    V-->>B: new AT + rotated RT (old RT now dead)
    B->>K: CAS write gen N→N+1 (cas = v)
    K-->>B: ok, version v+1
    Note over B: audit broker.refresh {gen N → N+1}
    B-->>C1: 200 AT(gen N+1)
    Note over B: lock released — C2 re-reads,<br/>sees gen N+1 with enough TTL
    B-->>C2: 200 AT(gen N+1) — same token,<br/>zero extra vendor calls
    Note over K: memory profile shown — the redis profile first<br/>CAS-writes a REFRESHING marker (§5), so the result is v+2
```

Waiter outcomes, in order of preference: generation advanced → serve the
winner's token; lock wait exceeds 10s → re-read once and serve if still
usable, else `503 vendor-unavailable` (retriable); winner hit
`invalid_grant` → the waiter finds STALE and answers `needs-consent`.

---

## 4. Proactive refresh — the sweeper

Runs every `SWEEP_INTERVAL_S=60` (leader-only + ±20% jitter in the redis
profile). Targets entries in the 5–15 minute band; the 0–5 minute band is
left to lazy resolve.

```mermaid
sequenceDiagram
    autonumber
    box rgba(99,102,241,0.20) Broker
        participant S as Sweeper (lease holder)
        participant B as Broker internals
    end
    box rgba(239,68,68,0.14) Coordination
        participant R as Redis
    end
    box rgba(234,179,8,0.16) Vendor
        participant V as Vendor AS
    end
    box rgba(16,185,129,0.16) Custody
        participant K as Custody
    end

    S->>R: SET vtb:sweep-lease NX PX 2×interval
    R-->>S: leader (others skip this pass)
    S->>K: list subjects per enabled vendor
    loop each entry, up to SWEEP_MAX_ENTRIES per pass (round-robin cursor)
        S->>K: read entry
        alt state REVOKE_PENDING
            Note over S: §8 — retry vendor revocation
        else REFRESHING abandoned (≥ REFRESHING_TTL_S) or ACTIVE and 300 < remaining ≤ 900
            S->>B: try_lock (non-blocking — a resolve may own it)
            B->>K: re-read under lock
            B->>V: refresh_token grant
            V-->>B: new AT + RT
            B->>K: CAS write gen+1
            Note over S: audit broker.refresh {path: proactive}
        else anything else
            Note over S: skip — not in the band
        end
    end
```

A failed entry never kills the loop (`broker.sweep.error` and continue).

---

## 5. Multi-replica refresh — persisted REFRESHING, takeover, CAS backstop

Redis profile only. The lock is the optimization; the custody CAS is the
correctness guarantee — a replica that dies mid-refresh can never corrupt
the stored pair.

```mermaid
sequenceDiagram
    autonumber
    box rgba(99,102,241,0.20) Broker replicas
        participant A as Replica A
        participant Bb as Replica B
    end
    box rgba(239,68,68,0.14) Coordination
        participant R as Redis
    end
    box rgba(234,179,8,0.16) Vendor
        participant V as Vendor AS
    end
    box rgba(16,185,129,0.16) Custody
        participant K as Custody
    end

    A->>R: SET vtb:lock:{vendor}:{sub} NX PX 20000
    R-->>A: acquired
    A->>K: CAS write state=REFRESHING + owner=A + started_at (v → v1)
    A->>V: refresh_token grant …
    Note over A: 💀 replica A dies here
    Bb->>R: SET NX … (retry 200ms±jitter, re-reading each try)
    Note over Bb: gives up after LOCK_TIMEOUT_S = 10s —<br/>that resolve gets 503 vendor-unavailable (entry REFRESHING)
    Note over R: lock expires after 20s TTL
    Bb->>R: a later resolve: SET NX …
    R-->>Bb: acquired
    Bb->>K: re-read: REFRESHING, owner=A
    alt marker fresh (< REFRESHING_TTL_S = 30s)
        Note over Bb: never re-refresh a fresh marker —<br/>serve the old AT if ≥ min_ttl,<br/>else 503 retry
    else marker abandoned (≥ 30s)
        Bb->>K: CAS write its own REFRESHING marker (v1 → v2)
        Bb->>V: refresh with the stored RT
        alt A's call never reached the vendor
            V-->>Bb: new AT + RT — clean takeover
            Bb->>K: CAS write ACTIVE gen+1 (cas = v2)
        else A's call consumed the RT before dying
            V-->>Bb: invalid_grant (replay burns the family)
            Bb->>K: CAS write STALE (cas = v2)
            Note over Bb: next resolve → needs-consent.<br/>Family lost, custody consistent.
        end
    end
    Note over A,K: if zombie A wakes and writes its outcome,<br/>its CAS (against v1) fails — the stale pair is discarded
```

---

## 6. Scope step-up — 409 needs-reconsent-scope

A grant exists but is narrower than the tool's `required_scopes`.
The caller can never widen past the registry ceiling.

```mermaid
sequenceDiagram
    autonumber
    box rgba(148,163,184,0.14) User side
        participant C as Client
    end
    box rgba(99,102,241,0.20) Broker
        participant B as Broker
    end
    box rgba(234,179,8,0.16) Vendor
        participant V as Vendor AS
    end
    box rgba(16,185,129,0.16) Custody
        participant K as Custody
    end

    C->>B: resolve {required_scopes: [read, write]}
    Note over B: ceiling check first: required ⊄ ceiling<br/>→ 403 scope-exceeds-ceiling (hard stop)
    B->>K: read entry — granted: [read]
    Note over B: missing = [write] →<br/>re-consent scopes = (held ∪ required) ∩ ceiling
    B-->>C: 409 needs-reconsent-scope<br/>{missing_scopes: [write], authorize_uri}
    Note over C: gateway turns this into a step-up<br/>authorization challenge
    C->>B: user re-runs the §1 dance via authorize_uri
    B->>V: … consent for [read, write] …
    B->>K: write fresh entry gen=1 (union of scopes)
    C->>B: retry resolve {required_scopes: [read, write]}
    B-->>C: 200
```

---

## 7. Death by vendor — STALE and the mass-STALE page

`invalid_grant` on refresh means the grant itself is gone: user revoked at
the vendor, refresh token expired from disuse, or the org uninstalled the
app.

```mermaid
sequenceDiagram
    autonumber
    box rgba(148,163,184,0.14) User side
        participant C as Client
    end
    box rgba(99,102,241,0.20) Broker
        participant B as Broker
    end
    box rgba(234,179,8,0.16) Vendor
        participant V as Vendor AS
    end
    box rgba(16,185,129,0.16) Custody
        participant K as Custody
    end
    box rgba(239,68,68,0.14) Ops
        participant O as On-call
    end

    C->>B: resolve (inside buffer)
    B->>V: refresh_token grant
    V-->>B: 400 invalid_grant
    B->>K: CAS write state=STALE
    Note over B: audit broker.stale {sub, vendor, generation}
    B-->>C: 404 needs-consent + authorize_uri
    Note over C: self-service recovery: re-run the §1 dance

    rect rgba(239,68,68,0.12)
        Note over B,O: mass event: ≥ 3 STALEs for ONE vendor<br/>inside a 60s window = org-uninstall signature
        B->>O: audit broker.stale.mass {page: true, security_event: true}
        Note over O: alert signal emitted once per window per vendor<br/>(deduped) — per-entry behavior unchanged
    end
```

A single STALE is expected hygiene, not an incident. Route the burst signal through monitoring if it should notify on-call.

---

## 8. Death by choice — revocation, vendor-first

Order matters: attempt vendor revocation before deleting custody. A vendor
without a revocation endpoint is deleted locally only, with
`vendor_revocation: "unsupported"`. Upstream access can survive that deletion.

```mermaid
sequenceDiagram
    autonumber
    box rgba(148,163,184,0.14) User side
        participant U as User
    end
    box rgba(99,102,241,0.20) Broker
        participant B as Broker
        participant S as Sweeper
    end
    box rgba(234,179,8,0.16) Vendor
        participant V as Vendor AS
    end
    box rgba(16,185,129,0.16) Custody
        participant K as Custody
    end

    U->>B: DELETE /v1/grants/{vendor}/{sub} (hub JWT)
    Note over B: sub in path MUST equal JWT sub<br/>(else 403 forbidden — strictly self-service)
    B->>K: read entry
    alt vendor reachable
        B->>V: RFC 7009 revoke (refresh token)²
        V-->>B: 200 — family dead at the vendor
        B->>K: delete entry (all versions + metadata)
        B-->>U: 200 {revoked: true}
    else vendor down
        B->>K: CAS write state=REVOKE_PENDING
        B-->>U: 502 revoke-pending (will retry)
        Note over B: resolves now answer 409 revoke-pending —<br/>the entry is unusable while parked
        loop each sweep pass until the vendor recovers
            S->>V: retry RFC 7009 revoke
        end
        V-->>S: 200
        S->>K: delete entry
        Note over S: audit broker.revoke {path: sweep-retry}
    end
```

² GitHub's documented deviation: grant-deletion API with basic auth
instead of RFC 7009 (`revocation.type: github_grant` in the registry).

---

## 9. Outages — fail closed, distinguishably

The two backends fail differently on purpose, and neither failure is ever
conflated with "no grant" (which would trigger a mass re-consent
stampede).

```mermaid
sequenceDiagram
    autonumber
    box rgba(148,163,184,0.14) User side
        participant C as Client
    end
    box rgba(99,102,241,0.20) Broker
        participant B as Broker
    end
    box rgba(239,68,68,0.14) Failed backends
        participant K as Custody (down)
        participant R as Redis (down)
    end

    rect rgba(234,179,8,0.10)
        Note over C,K: custody outage — total fail-closed
        C->>B: resolve (uncached sub)
        B--xK: read fails (3s bounded timeout)
        B-->>C: 503 vault-unavailable (retriable)
        Note over B: only grace: entries already in the<br/>≤ 60s per-replica cache keep serving
    end

    rect rgba(59,130,246,0.10)
        Note over C,R: redis outage — every path that needs redis (redis profile)
        C->>B: resolve, entry ACTIVE, sufficient scopes, NOT near expiry
        B-->>C: 200 — cache/custody path touches no redis
        C->>B: resolve, entry inside refresh buffer
        B--xR: lock acquisition fails
        B-->>C: 503 coordination-unavailable (retriable)
        Note over B: consent links need redis too: absent/STALE/<br/>insufficient-scope resolves also get 503, not 404/409
        Note over B: no lock ⇒ no refresh: refreshing without<br/>single-flight could burn a rotating RT family.<br/>Custody CAS still guards correctness regardless.
    end
```

Recovery resumes after dependencies return. Custody remains the durable
source of credentials; lost Redis consent records require users to start
a new browser flow.

---

## Reading the audit trail

Every transition above emits id-only JSON lines (never token material);
a lazy refresh, for example, logs both `broker.refresh` and `broker.resolve`. The joins:

| Event | Emitted in | Joins on |
|---|---|---|
| `broker.resolve` {decision, path} | §§2,3,6,7,9 | `hub_jti`, `sub`, `vendor` |
| `broker.consent.start` / `.complete` / `.fail` | §1, §6 | `sub`, `vendor`, `vendor_user_id` |
| `broker.refresh` {generation_from → to} | §§3,4,5 | `sub`, `vendor` |
| `broker.stale` / `broker.stale.mass` | §7 | `sub`/`vendor`; mass carries `page: true` |
| `broker.revoke` {outcome: revoked\|unsupported\|pending, path?: sweep-retry\|reconsent} | §8 | `sub`, `vendor`; `hub_jti` only on self-service DELETE |

A vendor-side action is traceable end-to-end: hub `jti` → `broker.resolve`
→ gateway record → vendor audit log via `vendor_user_id`.
