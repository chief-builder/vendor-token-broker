"""Custody storage on a real OpenBao (review change 7): subjects are one
encoded KV key each (even with "/"), pre-encoding entries are still served
and migrate on their next write, only 2 versions are retained, and STALE
entries hold no token material."""

from urllib.parse import quote

import requests
from stack import BROKER, MOCK, do_consent, mint, resolve, revoke_grant

from token_broker.custody import encode_sub

BAO = "http://localhost:8210"
ROOT = {"X-Vault-Token": "root"}


def kv(path: str) -> requests.Response:
    return requests.get(f"{BAO}/v1/vendor-tokens/{path}", headers=ROOT, timeout=10)


def keys_under(vendor: str) -> list[str]:
    r = requests.request(
        "LIST", f"{BAO}/v1/vendor-tokens/metadata/{vendor}", headers=ROOT, timeout=10
    )
    return r.json()["data"]["keys"] if r.status_code == 200 else []


def delete_grant(token: str, sub: str) -> requests.Response:
    return requests.delete(
        f"{BROKER}/v1/grants/mockhub/{quote(sub, safe='')}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )


def test_subject_with_slashes_is_one_key_and_fully_manageable():
    sub = "https://idp.example/users/42"
    tok = mint(sub)
    delete_grant(tok, sub)
    do_consent(tok)
    assert resolve(tok).status_code == 200
    keys = keys_under("mockhub")
    assert encode_sub(sub) in keys
    assert not [k for k in keys if k.startswith("https")]  # no nested folder
    grants = requests.get(
        f"{BROKER}/v1/grants", timeout=10, headers={"Authorization": f"Bearer {tok}"}
    ).json()["grants"]
    assert [g["vendor"] for g in grants] == ["mockhub"]
    r = delete_grant(tok, sub)
    assert r.status_code == 200, r.text
    assert encode_sub(sub) not in keys_under("mockhub")


def test_pre_encoding_entry_is_served_and_migrates_on_its_next_write():
    """An entry written by an older broker at the raw path keeps working;
    the next refresh moves it to the encoded path and removes the old one."""
    sub = "wf-legacy"
    tok = mint(sub)
    revoke_grant(tok)
    do_consent(tok)
    current = kv(f"data/mockhub/{encode_sub(sub)}").json()["data"]["data"]
    # Move it back to where an older broker would have left it.
    requests.post(
        f"{BAO}/v1/vendor-tokens/data/mockhub/{sub}",
        headers=ROOT,
        json={"data": current},
        timeout=10,
    ).raise_for_status()
    requests.delete(
        f"{BAO}/v1/vendor-tokens/metadata/mockhub/{encode_sub(sub)}", headers=ROOT, timeout=10
    ).raise_for_status()

    r = resolve(tok)  # 60s mock token: read legacy, refresh, CAS-migrate
    assert r.status_code == 200, r.text
    assert kv(f"data/mockhub/{sub}").status_code == 404
    migrated = kv(f"data/mockhub/{encode_sub(sub)}").json()["data"]["data"]
    assert migrated["refresh_generation"] == current["refresh_generation"] + 1
    revoke_grant(tok)


def test_only_two_versions_are_retained():
    sub = "wf-versions"
    tok = mint(sub)
    revoke_grant(tok)
    do_consent(tok)
    for _ in range(3):  # each resolve refreshes (60s mock tokens)
        assert resolve(tok).status_code == 200
    meta = kv(f"metadata/mockhub/{encode_sub(sub)}").json()["data"]
    assert meta["current_version"] >= 4
    assert len(meta["versions"]) <= 2
    assert kv("config").json()["data"]["max_versions"] == 2
    revoke_grant(tok)


def test_stale_entry_holds_no_token_material():
    sub = "wf-scrub"
    tok = mint(sub)
    revoke_grant(tok)
    do_consent(tok)
    requests.post(f"{MOCK}/_test/revoke_family", timeout=10)
    assert resolve(tok).status_code == 404
    stored = kv(f"data/mockhub/{encode_sub(sub)}").json()["data"]["data"]
    assert stored["state"] == "STALE"
    assert stored["access_token"] == "" and stored["refresh_token"] == ""
    assert delete_grant(tok, sub).status_code == 200  # nothing to revoke, still removable
