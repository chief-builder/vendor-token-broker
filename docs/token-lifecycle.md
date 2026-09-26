# Token lifecycle — sequence diagrams

This page shows every path a user's vendor connection (a "grant") can take
through the broker. It starts at first consent and ends at deletion. It also
shows what happens when things fail.

- A **grant** is the stored pair of vendor tokens for one user and one
  vendor (for example, one person's GitHub tokens).
- Timings are the defaults from `config.py`.
- For the rules behind these diagrams, see `design.md`. To try these same
  paths by hand, see `smoke-tests.md`.
- If you use the broker through the MCP gateway, see
  [the MCP gateway page](mcp-gateway.md) first. This page covers the engine
  underneath.

**Cast** (the same in every diagram):

| Actor | Role |
|---|---|
| Client | The trusted gateway that calls the broker. It sends the user's hub JWT (a signed sign-in token from the hub, your company's sign-in service). It is not the MCP client. |
| Broker | This service. One replica unless the diagram says otherwise. |
| Vendor AS | The vendor's authorization server, which issues vendor tokens. GitHub-class: it rotates refresh tokens. |
| Custody | OpenBao/Vault KV-v2, where tokens are stored. Each entry's KV version is the CAS handle (CAS = compare-and-swap: a write succeeds only if the version has not changed). |
| Redis | The coordination backend. Used only in the multi-replica (redis) profile. |

## 0. Orientation — the states and which diagram covers each transition

A grant is always in one of these states. The numbers (§1 to §8) point to
the diagram that shows each change.

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
| §1 | Birth | The consent flow writes `ACTIVE gen=1`. |
| §2–§4 | Steady state / refresh | The broker serves from cache, or refreshes on demand or ahead of time (`gen+1`). |
| §5 | Multi-replica | A stored `REFRESHING` marker, takeover by another replica, and the CAS backstop. |
| §6 | Scope step-up | Re-consent for the combined scopes writes a fresh `gen=1`. |
| §7 | STALE | `invalid_grant` leads to re-consent. A burst of these pages on-call. |
| §8 | Revocation | The broker revokes at the vendor first, then deletes. If the vendor is down, the entry waits in `REVOKE_PENDING`. |

`REFRESHING` exists only in the redis profile. Callers never see it:
`/v1/grants` reports it as `ACTIVE`.

---

## 1. Birth — first-time consent dance

This runs when a resolve finds no entry, or a STALE one, for `{vendor, sub}`.
(`sub` is the user's id from the hub JWT.)

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
    Note over B: check hub JWT again:<br/>only PS256/ES256, issuer,<br/>exactly one tier audience, mcp_contract
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
    Note over B: check ID token: signature, iss, aud, exp, nonce<br/>signed-in sub MUST equal the link's sub<br/>(else 403 + security event, no vendor step)
    Note over B: create vendor PKCE verifier (S256) and<br/>single-use state {sub, vendor, issuer,<br/>scopes ≤ registry ceiling, binding}<br/>record if the AS supports RFC 9207 iss
    B-->>UA: 307 → vendor authorize<br/>(client_id, code_challenge, state)
    UA->>V: user consents as themself
    V-->>UA: 302 → /v1/callback/{vendor}?code&state&iss
    UA->>B: GET /v1/callback/{vendor}?code&state&iss
    Note over B: 1. state exists, unused, vendor matches<br/>2. iss equals recorded issuer (exact match,<br/>   missing iss = mix-up if supported)<br/>3. binding cookie = the starting browser<br/>4. use up state, BEFORE redeeming the code
    B->>V: POST /token (code + PKCE verifier + client auth¹)
    V-->>B: access token + refresh token (rotating)
    B->>V: GET userinfo → vendor_user_id
    B->>K: write entry state=ACTIVE gen=1 (cas=None, re-consent overwrites)
    B-->>UA: "Connected — return to your client."
    C->>B: retry resolve
    B-->>C: 200 access_token
```

¹ Client auth follows the registry's `token_endpoint_auth_method`:
`client_secret_post`, `client_secret_basic`, or `private_key_jwt`. For
`private_key_jwt`, the broker signs an assertion with the key from
`vendor-clients/{vendor}` (RFC 7523).

How this flow protects the user:

- Only the user the link was made for can finish consent, and only in the
  browser that opened the link. The hub login and the binding cookie
  enforce this.
- The broker redeems the code on the server only.
- The browser sees only opaque state handles.
- The PKCE verifier appears only in server-side token requests.
- A replayed callback (state already used) gets a 400 **and** a
  `security_event: true` audit line.
- A changed or missing `iss` is rejected *before* the state is used up. So
  the real callback can still finish.

Standards: RFC 7523, RFC 9207.

---

## 2. Steady state — cache-hit resolve

This runs when the token has plenty of time left:
`expires_at − now ≥ max(min_ttl_s, REFRESH_BUFFER_S=300)`.

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
        Note over B: serve from memory, no custody read
    else cache miss/expired
        B->>K: read entry
        K-->>B: ACTIVE, expires_at, version
        Note over B: cache {entry, version} for ≤ CACHE_TTL_S
    end
    B-->>C: 200 {access_token, expires_at, granted_scopes}
```

- This is the hot path. Target: p99 ≤ 25 ms.
- The 60-second cache is also the **only** grace the broker gives during a
  custody outage (§9).

---

## 3. Lazy refresh — single-flight inside the buffer

This runs when the entry is ACTIVE but close to expiry:
`expires_at − now < max(min_ttl, 300)`.

Some vendors rotate refresh tokens. If a used refresh token is sent again,
they revoke the whole token family. So when many resolves arrive at once,
the broker must make exactly one vendor call ("single-flight").

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
    Note over B: lock per {vendor,sub}. C1 gets it.<br/>C2 waits (memory) or polls 200ms±jitter (redis)
    B->>K: re-read under lock (gen = N, version = v)
    B->>V: refresh_token grant (RT gen N)
    V-->>B: new AT + rotated RT (old RT now dead)
    B->>K: CAS write gen N→N+1 (cas = v)
    K-->>B: ok, version v+1
    Note over B: audit broker.refresh {gen N → N+1}
    B-->>C1: 200 AT(gen N+1)
    Note over B: lock released. C2 re-reads,<br/>sees gen N+1 with enough TTL
    B-->>C2: 200 AT(gen N+1), same token,<br/>zero extra vendor calls
    Note over K: memory profile shown. The redis profile first<br/>CAS-writes a REFRESHING marker (§5), so the result is v+2
```

What a waiting caller gets, best case first:

1. The generation moved forward: it gets the winner's token.
2. It waited more than 10s for the lock: it re-reads once. If the token is
   still usable, it gets it. If not, it gets `503 vendor-unavailable`
   (safe to retry).
3. The winner got `invalid_grant`: the waiter finds STALE and gets
   `needs-consent`.

---

## 4. Proactive refresh — the sweeper

The sweeper is a background loop. It refreshes tokens before callers need
them.

- It runs every `SWEEP_INTERVAL_S=60`. In the redis profile, only the leader
  runs it, with ±20% jitter.
- It targets entries with 5–15 minutes left.
- Entries with 0–5 minutes left are left to lazy refresh (§3).

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
            Note over S: §8, retry vendor revocation
        else REFRESHING abandoned (≥ REFRESHING_TTL_S) or ACTIVE and 300 < remaining ≤ 900
            S->>B: try_lock (no waiting, a resolve may hold it)
            B->>K: re-read under lock
            B->>V: refresh_token grant
            V-->>B: new AT + RT
            B->>K: CAS write gen+1
            Note over S: audit broker.refresh {path: proactive}
        else anything else
            Note over S: skip, not in the band
        end
    end
```

One failed entry never stops the loop. The sweeper logs
`broker.sweep.error` and moves on.

---

## 5. Multi-replica refresh — persisted REFRESHING, takeover, CAS backstop

This applies to the redis profile only.

- The lock is a speed-up. The custody CAS is what keeps data correct.
- A replica that dies mid-refresh can never corrupt the stored token pair.

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
    Note over Bb: gives up after LOCK_TIMEOUT_S = 10s.<br/>That resolve gets 503 vendor-unavailable (entry REFRESHING)
    Note over R: lock expires after 20s TTL
    Bb->>R: a later resolve: SET NX …
    R-->>Bb: acquired
    Bb->>K: re-read: REFRESHING, owner=A
    alt marker fresh (< REFRESHING_TTL_S = 30s)
        Note over Bb: never refresh over a fresh marker.<br/>Serve the old AT if ≥ min_ttl,<br/>else 503 retry
    else marker abandoned (≥ 30s)
        Bb->>K: CAS write its own REFRESHING marker (v1 → v2)
        Bb->>V: refresh with the stored RT
        alt A's call never reached the vendor
            V-->>Bb: new AT + RT, clean takeover
            Bb->>K: CAS write ACTIVE gen+1 (cas = v2)
        else A's call used the RT before dying
            V-->>Bb: invalid_grant (replay burns the family)
            Bb->>K: CAS write STALE (cas = v2)
            Note over Bb: next resolve → needs-consent.<br/>Family lost, custody consistent.
        end
    end
    Note over A,K: if A comes back and writes its result,<br/>its CAS (against v1) fails and the old pair is dropped
```

---

## 6. Scope step-up — 409 needs-reconsent-scope

This runs when a grant exists but lacks some of the tool's
`required_scopes`. The caller can never ask for more than the registry
ceiling (the most scopes the registry allows for that vendor).

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
    B->>K: read entry, granted: [read]
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

`invalid_grant` on refresh means the grant is gone at the vendor. Common
causes:

- the user revoked access at the vendor
- the refresh token expired from disuse
- the org uninstalled the app

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
    Note over C: user fixes it: re-run the §1 dance

    rect rgba(239,68,68,0.12)
        Note over B,O: mass event: ≥ 3 STALEs for ONE vendor<br/>within 60s = looks like an org uninstall
        B->>O: audit broker.stale.mass {page: true, security_event: true}
        Note over O: signal sent once per window per vendor<br/>(deduped). Each entry is handled as usual.
    end
```

- One STALE is normal and not an incident.
- To notify on-call about a burst, route the burst signal through your
  monitoring.

---

## 8. Death by choice — revocation, vendor-first

Order matters: the broker tries to revoke at the vendor before it deletes
from custody.

- If a vendor has no revocation endpoint, the broker deletes the entry
  locally only. It reports `vendor_revocation: "unsupported"`.
- In that case, access at the vendor can outlive the deletion.

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
    Note over B: sub in path MUST equal JWT sub<br/>(else 403 forbidden, users can only delete their own)
    B->>K: read entry
    alt vendor reachable
        B->>V: RFC 7009 revoke (refresh token)²
        V-->>B: 200, family dead at the vendor
        B->>K: delete entry (all versions + metadata)
        B-->>U: 200 {revoked: true}
    else vendor down
        B->>K: CAS write state=REVOKE_PENDING
        B-->>U: 502 revoke-pending (will retry)
        Note over B: resolves now get 409 revoke-pending.<br/>The entry cannot be used while it waits.
        loop each sweep pass until the vendor recovers
            S->>V: retry RFC 7009 revoke
        end
        V-->>S: 200
        S->>K: delete entry
        Note over S: audit broker.revoke {path: sweep-retry}
    end
```

² GitHub does this differently. It uses its grant-deletion API with basic
auth instead of RFC 7009 (`revocation.type: github_grant` in the registry).

---

## 9. Outages — fail closed, distinguishably

When a backend is down, the broker refuses rather than guesses ("fails
closed"). Each backend fails in its own, recognizable way.

Neither failure ever looks like "no grant". That would send every user
into re-consent at once.

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
        Note over C,K: custody outage, fully fail-closed
        C->>B: resolve (uncached sub)
        B--xK: read fails (3s timeout)
        B-->>C: 503 vault-unavailable (retriable)
        Note over B: only grace: entries already in the<br/>per-replica cache (CACHE_TTL_S) keep serving
    end

    rect rgba(59,130,246,0.10)
        Note over C,R: redis outage, every path that needs redis (redis profile)
        C->>B: resolve, entry ACTIVE, sufficient scopes, NOT near expiry
        B-->>C: 200, cache/custody path does not use redis
        C->>B: resolve, entry inside refresh buffer
        B--xR: lock acquisition fails
        B-->>C: 503 coordination-unavailable (retriable)
        Note over B: consent links need redis too. Absent, STALE or<br/>too-few-scopes resolves also get 503, not 404/409
        Note over B: no lock means no refresh. Refreshing without<br/>single-flight could burn a rotating RT family.<br/>Custody CAS still keeps data correct.
    end
```

- Normal service resumes when the backends come back.
- Custody stays the lasting store of credentials.
- If Redis loses consent records, users must start a new browser flow.

---

## Reading the audit trail

Every transition above writes JSON log lines with ids only, never token
material. One action can log more than one event. For example, a lazy
refresh logs both `broker.refresh` and `broker.resolve`.

How to link events together:

| Event | Emitted in | Joins on |
|---|---|---|
| `broker.resolve` {decision, path} | §§2,3,6,7,9 | `hub_jti`, `sub`, `vendor` |
| `broker.consent.start` / `.complete` / `.fail` | §1, §6 | `sub`, `vendor`, `vendor_user_id` |
| `broker.refresh` {generation_from → to} | §§3,4,5 | `sub`, `vendor` |
| `broker.stale` / `broker.stale.mass` | §7 | `sub`/`vendor`; mass carries `page: true` |
| `broker.revoke` {outcome: revoked\|unsupported\|pending, path?: sweep-retry\|reconsent} | §8 | `sub`, `vendor`; `hub_jti` only on self-service DELETE |

You can trace a vendor-side action from end to end: hub `jti` →
`broker.resolve` → gateway record → vendor audit log via `vendor_user_id`.
