"""The per-replica cache path (design §7) on the single-replica stack.

The mock normally issues 60s tokens, so every other suite takes the refresh
path. This module switches it to 1-hour tokens so resolves are served from
the cache, then checks the cache TTL (CACHE_TTL_S=10 in the test stack) and
that a broker-driven delete invalidates immediately."""
import time

import pytest
import requests
from stack import BROKER, MOCK, broker_audit, do_consent, mint, mock_state, resolve, revoke_grant

from token_broker.custody import encode_sub

BAO = "http://localhost:8210"
ROOT = {"X-Vault-Token": "root"}
CACHE_TTL_S = 10


@pytest.fixture(autouse=True, scope="module")
def _long_lived_tokens():
    requests.post(f"{MOCK}/_test/at_ttl", json={"seconds": 3600}, timeout=10).raise_for_status()
    yield
    requests.post(f"{MOCK}/_test/at_ttl", json={"seconds": 60}, timeout=10).raise_for_status()


def test_resolves_are_served_from_the_cache_without_refreshing():
    tok = mint("wf-cache-hit")
    revoke_grant(tok)
    do_consent(tok)
    refreshes = mock_state()["counters"]["token_refresh"]
    tokens = {resolve(tok).json()["access_token"] for _ in range(5)}
    assert len(tokens) == 1
    assert mock_state()["counters"]["token_refresh"] == refreshes
    paths = [e["path"] for e in broker_audit("broker.resolve", since="1m")
             if e.get("sub") == "wf-cache-hit" and e.get("decision") == "allow"]
    assert paths[-5:] == ["cache"] * 5
    revoke_grant(tok)


def test_cache_entry_expires_after_the_ttl():
    """A change made behind the broker's back is invisible until the cached
    copy expires, then visible: the documented ≤ CACHE_TTL_S staleness."""
    sub = "wf-cache-ttl"
    tok = mint(sub)
    revoke_grant(tok)
    do_consent(tok)
    first = resolve(tok).json()["access_token"]
    path = f"{BAO}/v1/vendor-tokens/data/mockhub/{encode_sub(sub)}"
    entry = requests.get(path, headers=ROOT, timeout=10).json()["data"]["data"]
    rotated = {**entry, "access_token": "rotated-out-of-band"}
    requests.post(path, headers=ROOT, json={"data": rotated}, timeout=10).raise_for_status()
    assert resolve(tok).json()["access_token"] == first            # still cached
    time.sleep(CACHE_TTL_S + 1)
    assert resolve(tok).json()["access_token"] == "rotated-out-of-band"
    revoke_grant(tok)


def test_broker_delete_invalidates_the_cache_at_once():
    tok = mint("wf-cache-del")
    revoke_grant(tok)
    do_consent(tok)
    assert resolve(tok).status_code == 200                          # cached now
    r = requests.delete(f"{BROKER}/v1/grants/mockhub/wf-cache-del",
                        headers={"Authorization": f"Bearer {tok}"}, timeout=15)
    assert r.status_code == 200
    assert resolve(tok).status_code == 404                          # no stale serve
