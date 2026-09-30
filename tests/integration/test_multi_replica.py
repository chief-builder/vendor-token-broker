"""Multi-replica proof (ADR-0001, marker: multi): two redis-coordinated
replicas behind round-robin nginx with no affinity.

Requires the multi compose profile:
  docker compose -f tests/stack/docker-compose.yml --profile multi up -d --build

Replica TTLs are tightened in compose (LOCK_TTL_MS=4000, REFRESHING_TTL_S=8,
SWEEP_INTERVAL_S=5) so death/takeover recovery is observable in test time.
"""

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from stack import MOCK_CONTAINER, container_audit_events, mint, mock_state, vendor_token_ttl

from token_broker.custody import encode_sub

LB = "http://localhost:8400"
A = "http://localhost:8401"
B = "http://localhost:8402"
BAO = "http://localhost:8210"
REPLICAS = ["vtb-broker-a", "vtb-broker-b"]


def _lb_up() -> bool:
    try:
        return requests.get(f"{LB}/healthz", timeout=2).ok
    except requests.RequestException:
        return False


pytestmark = [
    pytest.mark.multi,
    pytest.mark.skipif(
        not _lb_up(),
        reason="multi profile not running (docker compose --profile multi up -d --build)",
    ),
]


@pytest.fixture(scope="module", autouse=True)
def _replicas_only():
    """Stop the single-replica broker for the module: it shares custody and
    its memory-backend sweeper would double-handle REVOKE_PENDING retries."""
    subprocess.run(["docker", "stop", "vtb-broker"], capture_output=True)
    yield
    subprocess.run(["docker", "start", "vtb-broker"], capture_output=True)


def resolve_at(
    base: str,
    token: str,
    min_ttl_s: int = 30,
    timeout: int = 20,
) -> requests.Response:
    return requests.post(
        f"{base}/v1/tokens/resolve",
        json={"vendor": "mockhub", "min_ttl_s": min_ttl_s},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )


def revoke_at(base: str, token: str) -> requests.Response:
    import base64

    part = token.split(".")[1]
    sub = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["sub"]
    return requests.delete(
        f"{base}/v1/grants/mockhub/{sub}", headers={"Authorization": f"Bearer {token}"}, timeout=30
    )


def do_consent_at(base: str, token: str) -> None:
    r = resolve_at(base, token)
    if r.status_code == 200:
        return
    assert r.status_code == 404, r.text
    uri = r.json()["authorize_uri"].replace(":8400", f":{base.rsplit(':', 1)[1]}")
    page = requests.get(uri, timeout=15)
    assert page.status_code == 200 and "Connected" in page.text, page.text
    assert resolve_at(base, token).status_code == 200


def replica_audit(event: str, since: str = "5m") -> list[dict]:
    events = []
    for c in REPLICAS:
        events.extend(container_audit_events(c, since))
    return [e for e in events if e.get("audit") == event]


def bao_read_entry(sub: str) -> dict:
    r = requests.get(
        f"{BAO}/v1/vendor-tokens/data/mockhub/{encode_sub(sub)}",
        headers={"X-Vault-Token": "root"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["data"]["data"]


def bao_write_entry(sub: str, entry: dict) -> None:
    r = requests.post(
        f"{BAO}/v1/vendor-tokens/data/mockhub/{encode_sub(sub)}",
        headers={"X-Vault-Token": "root"},
        json={"data": entry},
        timeout=10,
    )
    r.raise_for_status()


def _pause(container: str) -> None:
    subprocess.run(["docker", "pause", container], check=True, capture_output=True)


def _unpause(container: str) -> None:
    subprocess.run(["docker", "unpause", container], capture_output=True)


# ── headline: single-flight across replicas through the LB ──────────────────


def test_lb_twenty_parallel_one_refresh_zero_replays():
    """As in test_refresh.py: the consent's 60 s token forces one refresh,
    which mints a long-lived token, so a late starter is served rather than
    legitimately refreshing a second time."""
    tok = mint("wf-multi-flight")
    revoke_at(LB, tok)
    do_consent_at(LB, tok)
    before = mock_state()["counters"]

    with vendor_token_ttl(3600), ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: resolve_at(LB, tok), range(20)))

    assert all(r.status_code == 200 for r in results), [r.status_code for r in results]
    after = mock_state()["counters"]
    refreshes = after["token_refresh"] - before["token_refresh"]
    assert refreshes == 1, f"expected exactly one vendor refresh, saw {refreshes}"
    assert after["rt_replay"] - before["rt_replay"] == 0, (
        "a replay means the rotating token family was burned across replicas"
    )
    revoke_at(LB, tok)


# ── consent state is replica-agnostic (no affinity) ─────────────────────────


def test_authorize_on_a_callback_on_b():
    tok = mint("wf-cross-consent")
    revoke_at(LB, tok)
    r = resolve_at(A, tok)
    assert r.status_code == 404
    # Authorize on A, finish the hub login on B, the vendor leg on A again:
    # every leg's state is shared through redis (no affinity), and the one
    # browser carries the binding cookie across replicas (cookies ignore ports).
    browser = requests.Session()
    auth_a = r.json()["authorize_uri"].replace(":8400", ":8401")
    to_hub = browser.get(auth_a, allow_redirects=False, timeout=15)
    assert to_hub.status_code in (302, 307), to_hub.text
    back_from_hub = browser.get(to_hub.headers["location"], allow_redirects=False, timeout=15)
    assert ":8400/v1/callback/_hub" in back_from_hub.headers["location"]
    to_vendor = browser.get(
        back_from_hub.headers["location"].replace(":8400", ":8402"),
        allow_redirects=False,
        timeout=15,
    )
    assert to_vendor.status_code in (302, 307), to_vendor.text
    back = browser.get(to_vendor.headers["location"], allow_redirects=False, timeout=15)
    assert back.status_code in (302, 307)
    callback_url = back.headers["location"]
    assert ":8400" in callback_url  # redirect_uri is the LB, as the vendor saw it
    page = browser.get(callback_url.replace(":8400", ":8401"), timeout=15)
    assert page.status_code == 200 and "Connected" in page.text, page.text
    assert resolve_at(B, tok).status_code == 200
    assert resolve_at(A, tok).status_code == 200
    revoke_at(LB, tok)


# ── cross-replica cache invalidation (pub/sub) ──────────────────────────────


def test_cross_replica_cache_invalidation():
    sub = "wf-inval"
    now = time.time()
    bao_write_entry(
        sub,
        {
            "access_token": "cached-token-inval",
            "refresh_token": "unused",
            "expires_at": now + 3600,
            "granted_scopes": ["issues:read"],
            "vendor_user_id": "mock-4217",
            "state": "ACTIVE",
            "refresh_generation": 1,
            "last_refresh_at": now,
            "created_at": now,
        },
    )
    tok = mint(sub)
    r = resolve_at(A, tok)  # A caches the long-lived entry (cache path)
    assert r.status_code == 200 and r.json()["access_token"] == "cached-token-inval"
    # Revoke on B: custody delete + invalidation broadcast.
    assert revoke_at(B, tok).status_code == 200
    # Without the broadcast, A would serve its cache for up to 60s. With it,
    # A drops the entry as soon as the pub/sub message lands.
    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        status = resolve_at(A, tok).status_code
        if status == 404:
            break
        time.sleep(0.2)
    assert status == 404, "replica A kept serving a revoked grant from cache"


# ── abandoned-REFRESHING takeover (lock-TTL death recovery) ─────────────────


def test_abandoned_refreshing_takeover():
    tok = mint("wf-takeover")
    revoke_at(LB, tok)
    do_consent_at(LB, tok)
    entry = bao_read_entry("wf-takeover")  # holds the live rotating RT
    # Simulate a replica that died mid-refresh past the REFRESHING TTL: the
    # marker is abandoned and the old access token is too short to serve.
    bao_write_entry(
        "wf-takeover",
        {
            **entry,
            "state": "REFRESHING",
            "refresh_owner": "dead-replica",
            "refresh_started_at": time.time() - 60,
            "expires_at": time.time() + 20,
        },
    )  # < min_ttl -> must refresh, not serve
    gen_before = entry["refresh_generation"]
    r = resolve_at(LB, tok)
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] != entry["access_token"]
    stored = bao_read_entry("wf-takeover")
    assert stored["state"] == "ACTIVE"
    assert stored["refresh_generation"] == gen_before + 1
    revoke_at(LB, tok)


# ── single sweep leader ──────────────────────────────────────────────────────


def test_single_sweep_leader_retries_revoke_once():
    started = time.time()
    tok = mint("wf-sweep-lead")
    revoke_at(LB, tok)
    do_consent_at(LB, tok)
    _pause(MOCK_CONTAINER)
    try:
        r = revoke_at(LB, tok)  # vendor down -> REVOKE_PENDING (502)
        assert r.status_code == 502, r.text
    finally:
        _unpause(MOCK_CONTAINER)

    # Only the lease holder sweeps: exactly one retry must revoke + delete.
    # Generous deadline: a lease handoff while the vendor was paused can block
    # one pass for the full 10s vendor timeout. The proof is exactly-once,
    # not latency.
    def _retries() -> list[dict]:
        # ts filter: the sub is reused across runs and docker-log windows
        # overlap; only this run's events count.
        return [
            e
            for e in replica_audit("broker.revoke", since="3m")
            if e.get("sub") == "wf-sweep-lead"
            and e.get("path") == "sweep-retry"
            and e.get("ts", 0) >= started
        ]

    deadline = time.time() + 60
    events = []
    while time.time() < deadline:
        events = _retries()
        if events:
            time.sleep(8)  # one more interval: a second retry would land now
            events = _retries()
            break
        time.sleep(1)
    assert len(events) == 1, f"expected exactly one sweep-retry revoke, saw {events}"
    assert resolve_at(LB, tok).status_code == 404  # entry gone -> needs-consent


# ── redis loss: refresh path fails closed, cache hits still serve ───────────


def test_redis_down_503_on_refresh_path_while_cache_hits_serve():
    now = time.time()
    cache_sub, refresh_sub = "wf-redis-cache", "wf-redis-refresh"
    bao_write_entry(
        cache_sub,
        {
            "access_token": "cached-token-redis",
            "refresh_token": "unused",
            "expires_at": now + 3600,
            "granted_scopes": ["issues:read"],
            "vendor_user_id": "mock-4217",
            "state": "ACTIVE",
            "refresh_generation": 1,
            "last_refresh_at": now,
            "created_at": now,
        },
    )
    cache_tok, refresh_tok = mint(cache_sub), mint(refresh_sub)
    revoke_at(LB, refresh_tok)
    do_consent_at(LB, refresh_tok)  # 60s vendor token: always in buffer
    for _ in range(4):  # warm BOTH replica caches via RR
        assert resolve_at(LB, cache_tok).status_code == 200

    _pause("vtb-redis")
    try:
        for _ in range(4):  # every replica must keep serving the cache hit
            r = resolve_at(LB, cache_tok)
            assert r.status_code == 200
            assert r.json()["access_token"] == "cached-token-redis"
        r = resolve_at(LB, refresh_tok)  # refresh path needs the lock
        assert r.status_code == 503, r.text
        assert r.json()["title"] == "coordination-unavailable"
    finally:
        _unpause("vtb-redis")
    time.sleep(1)
    assert resolve_at(LB, refresh_tok).status_code == 200
    revoke_at(LB, refresh_tok)
    revoke_at(LB, cache_tok)


# ── replica death mid-refresh: service recovers, custody never corrupts ─────


def test_replica_death_mid_refresh_recovers():
    tok = mint("wf-death")
    revoke_at(LB, tok)
    do_consent_at(LB, tok)
    time.sleep(1)
    _pause(MOCK_CONTAINER)  # make A's refresh hang at the vendor
    fired = threading.Thread(
        target=lambda: _swallow(lambda: resolve_at(A, tok, timeout=30)), daemon=True
    )
    fired.start()
    time.sleep(1.5)  # A holds the lock + persisted REFRESHING
    _pause("vtb-broker-a")  # kill the lock holder mid-flight
    _unpause(MOCK_CONTAINER)
    try:
        # B must become serviceable within lock TTL (4s) + REFRESHING TTL (8s).
        deadline, final = time.time() + 30, None
        while time.time() < deadline:
            r = resolve_at(B, tok)
            if r.status_code in (200, 404):
                final = r
                break
            time.sleep(1)
        assert final is not None, "replica B never recovered from A's death"
        if final.status_code == 404:
            # The takeover replayed a consumed RT and the vendor burned the
            # family -> STALE -> re-consent. That IS the documented recovery:
            # CAS + STALE guarantee custody integrity, not the token family.
            uri = final.json()["authorize_uri"].replace(":8400", ":8402")
            page = requests.get(uri, timeout=15)
            assert page.status_code == 200 and "Connected" in page.text
        assert resolve_at(B, tok).status_code == 200
    finally:
        _unpause("vtb-broker-a")
    # Whole stack healthy again. Either A's zombie write lost the CAS, or
    # A's refresh timed out client-side after the vendor had already
    # processed it: A then restored the used refresh token, B's next refresh
    # replayed it, and the vendor burned the family. That is the documented
    # worst case (design §9: STALE, then re-consent); custody stays intact.
    deadline = time.time() + 15
    while time.time() < deadline:
        if _lb_up():
            r = resolve_at(LB, tok)
            if r.status_code == 200:
                break
            if r.status_code == 404:
                uri = r.json()["authorize_uri"].replace(":8400", ":8402")
                page = requests.get(uri, timeout=15)
                assert page.status_code == 200 and "Connected" in page.text
        time.sleep(1)
    assert resolve_at(LB, tok).status_code == 200
    stored = bao_read_entry("wf-death")
    assert stored["state"] == "ACTIVE"
    revoke_at(LB, tok)


def _swallow(fn):
    try:
        fn()
    except Exception:
        pass
