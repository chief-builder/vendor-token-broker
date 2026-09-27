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
    ACTIVE --> ACTIVE : §3–§4 refresh gen+1<br/>§6 step-up gen=1
    ACTIVE --> REFRESHING : §5 marker (redis)
    REFRESHING --> ACTIVE : §5 CAS ok<br/>or vendor down
    REFRESHING --> STALE : §5 replay
    ACTIVE --> STALE : §7 invalid_grant<br/>or no refresh token
    STALE --> ACTIVE : §7 re-consent
    ACTIVE --> NoGrant : §8 revoke
    ACTIVE --> REVOKE_PENDING : §8 vendor down
    REFRESHING --> REVOKE_PENDING : §8 vendor down
    REVOKE_PENDING --> NoGrant : §8 sweep retry
    REVOKE_PENDING --> ACTIVE : §1 re-consent,<br/>old grant revoked first

    class ACTIVE live
    class REFRESHING transient
    class STALE,REVOKE_PENDING dead
```

What to notice:

- A refresh can go STALE from a resolve (§3) or from the sweeper (§4).
- DELETE parks whatever state it read as `REVOKE_PENDING`. A STALE entry
  holds no tokens, so DELETE removes it with no vendor call.
- Consent overwrites any state with a fresh `gen=1`. Only a
  `REVOKE_PENDING` predecessor is revoked at the vendor first.

| Transition group | Diagram | What happens |
|---|---|---|
| §1 | Birth | The consent flow writes `ACTIVE gen=1`. |
| §2–§4 | Steady state / refresh | The broker serves from cache, or refreshes on demand or ahead of time (`gen+1`). |
| §5 | Multi-replica | A stored `REFRESHING` marker, takeover by another replica, and the CAS backstop. |
| §6 | Scope step-up | Re-consent for the combined scopes writes a fresh `gen=1`. |
| §7 | STALE | `invalid_grant`, or no refresh token, leads to re-consent. A burst of `invalid_grant` pages on-call. |
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
    Note over B: check hub JWT again:<br/>pinned algorithm (default PS256/ES256), issuer,<br/>exactly one tier audience, mcp_contract
    B->>K: read entry
    K-->>B: not found
    B-->>C: 404 needs-consent + authorize_uri(txn)
    C->>UA: open authorize_uri
    UA->>B: GET /v1/authorize/{vendor}?txn=…
    Note over B: link used up (single use, ≤ 5 min old)<br/>set binding cookie (HttpOnly, SameSite=Lax)
    B-->>UA: 307 → hub login (PKCE, nonce,<br/>login_hint=sub unless HUB_LOGIN_HINT=none)
    UA->>H: user signs in
    H-->>UA: 302 → /v1/callback/_hub?code&state
    UA->>B: GET /v1/callback/_hub (with binding cookie)
    Note over B: login state valid, binding cookie matches,<br/>iss = HUB_ISSUER if present, then use up state
    B->>H: POST /token (code + PKCE verifier)
    H-->>B: ID token
    Note over B: check ID token: signature, iss, aud, exp, nonce<br/>signed-in sub MUST equal the link's sub<br/>(else 403 + security event, no vendor step)
    Note over B: create vendor PKCE verifier (S256) and<br/>single-use state {sub, vendor, issuer,<br/>scopes ≤ registry ceiling, binding}<br/>record if the AS supports RFC 9207 iss
    B-->>UA: 307 → vendor authorize<br/>(client_id, code_challenge, state,<br/>resource if the registry sets one)
    UA->>V: user consents as themself
    V-->>UA: 302 → /v1/callback/{vendor}?code&state&iss
    UA->>B: GET /v1/callback/{vendor}?code&state&iss
    Note over B: 1. state exists, unused, vendor matches<br/>2. iss equals recorded issuer (exact match,<br/>   missing iss = mix-up if supported)<br/>3. binding cookie = the starting browser<br/>4. use up state, BEFORE redeeming the code<br/>5. a REVOKE_PENDING predecessor is revoked first
    B->>V: POST /token (code + PKCE verifier<br/>+ client auth¹ + resource if set)
    V-->>B: access token + refresh token (rotating)
    B->>V: GET user endpoint → vendor_user_id<br/>(best effort, "unknown" on failure)
    B->>K: write entry state=ACTIVE gen=1 (cas=None, re-consent overwrites)
    Note over B: drop cached entry on every replica
    B-->>UA: "Connected — return to your client."
    C->>B: retry resolve
    B-->>C: 200 access_token
```

¹ Client auth follows the registry's `token_endpoint_auth_method`:
`client_secret_post`, `client_secret_basic`, or `private_key_jwt`. For
`private_key_jwt`, the broker signs an assertion with the key from
`vendor-clients/{vendor}` (RFC 7523). Its audience is the vendor's token
endpoint and it lives 300 seconds.

The `resource` parameter (RFC 8707) is for vendors whose tokens are bound
to one MCP server, such as Atlassian and Cloudflare. When the registry
entry sets `resource`, the broker sends it on the vendor authorize
redirect, on the code exchange, and on every refresh (§3, §4).

If the custody write fails after the code was redeemed, the broker
revokes the new grant at the vendor (best effort) and shows a 503 page.
The user connects again.

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
    Note over B: lock released. C2 re-reads,<br/>sees gen N+1 and serves it
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

In case 1 the waiter gets the winner's token even when it has less than
`min_ttl_s` left. The audit line then carries `short_ttl: true`. Another
refresh could not give a longer-lived token. (`min_ttl_s` itself is capped
at `REFRESH_BUFFER_S`, and a cap is audited as `min_ttl_clamped_from`.)

The refresh request carries the stored refresh token, client auth, and
`resource` when the registry sets one. If the vendor is unreachable or
answers any error other than `invalid_grant`/`bad_refresh_token` (for
example `invalid_client`), the caller gets `503 vendor-unavailable` and
the entry keeps its tokens.

---

## 4. Proactive refresh — the sweeper

The sweeper is a background loop. It refreshes tokens before callers need
them.

- It runs every `SWEEP_INTERVAL_S=60` (`0` turns it off). In the redis
  profile, only the lease holder runs it, with ±20% jitter. In the memory
  profile the single replica always runs it, on a fixed interval.
- It targets ACTIVE entries with 5–15 minutes left
  (`REFRESH_BUFFER_S < remaining ≤ PROACTIVE_REFRESH_S`, 300–900 s), and
  REFRESHING markers abandoned for `REFRESHING_TTL_S` (30 s).
- Entries with 0–5 minutes left are left to lazy refresh (§3).
- It retries pending revocations (§8).

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
            S->>V: retry revoke (no lock, no CAS)
            Note over S: success or unsupported: delete entry,<br/>audit broker.revoke {path: sweep-retry}.<br/>Vendor still down: try next pass
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

- The sweeper never waits for a lock. If a resolve holds it, the entry is
  skipped this pass.
- A proactive refresh ends like a lazy one: `gen+1`, STALE on
  `invalid_grant` or a missing refresh token (§7), or no change when the
  vendor is down.
- If Redis is down, the whole tick is skipped. If custody cannot list one
  vendor's entries, that vendor is skipped for the pass.

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

- The re-consent scopes keep the ceiling's order, and never drop a scope
  the user already granted.
- If the vendor's `scope_ceiling` is empty (a GitHub App, where the app's
  permissions decide), the broker ignores `required_scopes`, so this path
  never happens.
- If the vendor grants more than the ceiling, the extra scopes are left
  out of the entry and listed as `scope_widened` in the audit line.

---

## 7. Death by vendor — STALE and the mass-STALE page

`invalid_grant` on refresh (GitHub says `bad_refresh_token`, treated the
same) means the grant is gone at the vendor. Common causes:

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
    B->>K: CAS write state=STALE, both tokens blanked
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
- An entry with no refresh token goes STALE when it reaches the refresh
  buffer, with no vendor call. It is audited as `broker.stale` but never
  counted toward the burst.
- If the STALE write loses its CAS (a concurrent refresh or re-consent
  moved the entry), nothing is marked or counted.
- If Redis is down, the per-entry `broker.stale` is still written, but the
  burst is not counted.
- To notify on-call about a burst, route the burst signal through your
  monitoring.

---

## 8. Death by choice — revocation, vendor-first

Order matters: the broker tries to revoke at the vendor before it deletes
from custody.

- The MCP gateway's `disconnect_<service>` tools call this same DELETE
  with the person's hub JWT (see [the MCP gateway page](mcp-gateway.md)).
- If a vendor has no revocation endpoint, the broker deletes the entry
  locally only. It reports `vendor_revocation: "unsupported"`.
- In that case, access at the vendor can outlive the deletion.

```mermaid
sequenceDiagram
    autonumber
    box rgba(148,163,184,0.14) User side
        participant U as User or gateway
    end
    box rgba(99,102,241,0.20) Broker
        participant B as Broker
        participant L as Refresh lock
    end
    box rgba(234,179,8,0.16) Vendor
        participant V as Vendor AS
    end
    box rgba(16,185,129,0.16) Custody
        participant K as Custody
    end

    U->>B: DELETE /v1/grants/{vendor}/{sub} (hub JWT)
    Note over B: sub in path MUST equal JWT sub<br/>(else 403 forbidden, users can only delete their own)
    B->>L: wait for the lock (≤ LOCK_TIMEOUT_S = 10s)
    Note over B,L: timeout → 503 vendor-unavailable<br/>Redis down → 503 coordination-unavailable
    B->>K: read entry (version v)
    loop up to 3 rounds
        B->>V: revoke (refresh token)²
        B->>K: re-read
        Note over B,K: same version v → done.<br/>A newer pair landed → revoke that one too
    end
    alt revoked, or vendor has no revocation endpoint
        B->>K: delete entry (all versions + metadata)
        B-->>U: 200 {revoked: true} (+ vendor_revocation: unsupported)
    else vendor down
        B->>K: CAS write the read entry as REVOKE_PENDING (cas = v)
        B-->>U: 502 revoke-pending (will retry)
    end
    B->>L: release the lock
```

What to notice:

- The lock stops a refresh from rotating the pair between the revoke and
  the delete. The re-read loop covers a lost lock. If the pair still
  changes after 3 rounds, the answer is 503 `vendor-unavailable` and
  nothing is deleted.
- A parked entry cannot be used: resolves get 409 `revoke-pending`. If
  the park CAS loses, the broker re-reads once and parks the newer pair.
- Every finished delete drops the entry from every replica's cache.

The sweeper then retries the parked revocation on each pass (§4):

```mermaid
sequenceDiagram
    autonumber
    participant S as Sweeper
    participant V as Vendor AS
    participant K as Custody

    loop each sweep pass until the vendor recovers
        S->>K: read entry, state REVOKE_PENDING
        S->>V: retry revoke (no lock, no CAS)
    end
    V-->>S: 200
    S->>K: delete entry
    Note over S: audit broker.revoke {path: sweep-retry}
```

If the user connects again while the entry is `REVOKE_PENDING`, the
consent callback revokes and removes it first (`path: reconsent`). If the
vendor is still down, consent stops with a 503 page and the user tries
again later.

² GitHub does this differently. It uses its grant-deletion API with basic
auth instead of RFC 7009 (`revocation.type: github_grant` in the registry).
Other vendors get the refresh token, or the access token for a grant that
never expires. A STALE entry holds no tokens, so it is deleted with no
vendor call.

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

Other paths during a Redis outage (redis profile):

| Path | Answer |
|---|---|
| `GET /v1/authorize/{vendor}` | problem JSON 503 `coordination-unavailable` |
| `DELETE /v1/grants/{vendor}/{sub}` | problem JSON 503 `coordination-unavailable` |
| Hub callback, starting the vendor step | problem JSON 503 `coordination-unavailable` |
| Browser callbacks (`/v1/callback/_hub`, `/v1/callback/{vendor}`) reading or using up state | HTML page "Coordination store unavailable." with status 503, no problem `title` |
| Sweeper | skips the tick |

- Normal service resumes when the backends come back.
- Custody stays the lasting store of credentials.
- If Redis loses consent records, users must start a new browser flow.

---

## Reading the audit trail

Every transition above writes JSON log lines with ids only, never token
material. One action can log more than one event. For example, a lazy
refresh logs both `broker.refresh` and `broker.resolve`. Every line has
`audit` (the event name) and `ts` (Unix time).

How to link events together:

| Event | Emitted in | Joins on |
|---|---|---|
| `broker.resolve` {decision, path} | §§2,3,6,7,9 | `hub_jti`, `sub`, `vendor` |
| `broker.consent.start` / `.complete` / `.fail` | §1, §6 | `sub`, `vendor`, `vendor_user_id` |
| `broker.refresh` {generation_from → to, path?: proactive} | §§3,4,5 | `sub`, `vendor` |
| `broker.stale` / `broker.stale.mass` | §§3,4,7 | `sub`/`vendor`. Mass carries `page: true` |
| `broker.revoke` {outcome: revoked\|unsupported\|pending, path?: sweep-retry\|reconsent} | §8 | `sub`, `vendor`. `hub_jti` only on a self-service DELETE that finished (`revoked` or `unsupported`), not on `pending` |

You can trace a vendor-side action from end to end: hub `jti` →
`broker.resolve` → gateway record → vendor audit log via `vendor_user_id`.
