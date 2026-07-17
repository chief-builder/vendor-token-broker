"""Security invariants against the deployed broker (design §3/§10/§11):
no-issuance surface, the hub bad-token matrix, fail-closed custody, and the
token-in-log grep. Ported from the lab's phase7 red-team probes."""
import subprocess

import pytest
import requests
from stack import (
    BROKER,
    OPENBAO_CONTAINER,
    do_consent,
    grep_container_logs,
    mint,
    resolve,
    wait_for,
)

# ── No-issuance rule (design §11) ────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/oauth/token", "/token", "/keys", "/v1/tokens/issue",
    "/.well-known/jwks.json", "/.well-known/openid-configuration",
])
def test_broker_exposes_no_issuance_endpoint(path):
    """The broker is a custodian, not an issuer: no token or JWKS surface."""
    assert requests.get(f"{BROKER}{path}", timeout=10).status_code == 404


# ── Hub validation at the door (design §3) ───────────────────────────────────

def test_resolve_without_token_is_401():
    r = requests.post(f"{BROKER}/v1/tokens/resolve",
                      json={"vendor": "mockhub", "min_ttl_s": 30}, timeout=15)
    assert r.status_code == 401


@pytest.mark.parametrize("kind", [
    "rs256",            # forbidden algorithm on a resolvable key
    "wrong_issuer",
    "external_tier",    # wrong tier audience
    "two_tiers",        # cross-tier token: exactly one tier audience required
    "expired",
    "no_jti",
    "wrong_contract",
    "no_contract",
])
def test_bad_hub_token_matrix(kind):
    token = mint("wf-eve", kind=kind)
    r = resolve(token)
    assert r.status_code == 401, f"{kind}: expected 401, got {r.status_code}"
    assert r.json()["title"] == "invalid-hub-token"


def test_garbage_token_is_401():
    r = requests.post(f"{BROKER}/v1/tokens/resolve",
                      json={"vendor": "mockhub"},
                      headers={"Authorization": "Bearer not.a.jwt"}, timeout=15)
    assert r.status_code == 401


# ── Fail-closed custody (design §10) ─────────────────────────────────────────

def test_custody_loss_fails_closed():
    # PAUSE (not stop): dev-mode OpenBao holds all provisioning in memory, so
    # a stop/start would wipe the broker token and vendor creds. Pausing
    # freezes it — unreachable to the broker, state intact on unpause.
    fresh = mint("wf-uncached")  # no cached entry -> forces a custody read
    subprocess.run(["docker", "pause", OPENBAO_CONTAINER], check=True,
                   capture_output=True)
    try:
        r = resolve(fresh)
        assert r.status_code == 503
        assert "vault" in r.json()["type"]
    finally:
        subprocess.run(["docker", "unpause", OPENBAO_CONTAINER], check=True,
                       capture_output=True)
    wait_for(lambda: requests.get(f"{BROKER}/healthz", timeout=5).ok,
             timeout=60, what="broker healthy after openbao unpause")


# ── Token-in-log grep (design §11) ───────────────────────────────────────────

def test_no_token_material_in_any_log(alice):
    """A live hub token and a live vendor access token must appear nowhere in
    any stack container log (records carry ids, never secrets)."""
    do_consent(alice)
    r = resolve(alice)
    assert r.status_code == 200, r.text
    vendor_at = r.json()["access_token"]
    hub_sig = alice.rsplit(".", 1)[-1]  # the JWT signature segment

    for label, needle in (("vendor access_token", vendor_at),
                          ("hub jwt signature", hub_sig)):
        hits = grep_container_logs(needle)
        assert not hits, f"{label} found in container logs: {hits}"
