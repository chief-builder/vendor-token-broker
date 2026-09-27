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

# ── No-issuance rule (design §10) ────────────────────────────────────────────

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


# ── Fail-closed custody (design §9) ──────────────────────────────────────────

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


# ── Token-in-log grep (design §10) ───────────────────────────────────────────

def test_no_token_material_in_any_log(alice):
    """No secret the stack handles appears in any container log: the hub
    JWT, the vendor access and refresh tokens, the vendor client secret, and
    the private_key_jwt signing key (records carry ids, never secrets)."""
    from pathlib import Path

    from stack import sub_of

    from token_broker.custody import encode_sub

    do_consent(alice)
    r = resolve(alice)
    assert r.status_code == 200, r.text
    vendor_at = r.json()["access_token"]
    hub_sig = alice.rsplit(".", 1)[-1]  # the JWT signature segment
    entry = requests.get(
        f"http://localhost:8210/v1/vendor-tokens/data/mockhub/{encode_sub(sub_of(alice))}",
        headers={"X-Vault-Token": "root"}, timeout=10).json()["data"]["data"]
    key_pem = (Path(__file__).resolve().parents[1] / "stack" / "keys" /
               "mockhub-jwt-private.pem").read_text().splitlines()
    key_line = next(line for line in key_pem[1:] if len(line) > 40)

    for label, needle in (("vendor access_token", vendor_at),
                          ("vendor refresh_token", entry["refresh_token"]),
                          ("hub jwt signature", hub_sig),
                          ("vendor client secret", "mock-secret"),
                          ("pkj private key", key_line)):
        assert needle, label
        hits = grep_container_logs(needle)
        assert not hits, f"{label} found in container logs: {hits}"
