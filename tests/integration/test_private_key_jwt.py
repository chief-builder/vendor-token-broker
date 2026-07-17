"""private_key_jwt leg (design §3, RFC 7523): the mockhub-jwt registry entry
drives consent, refresh, and RFC 7009 revocation with a client assertion the
mock vendor verifies for real."""
import requests
from stack import BROKER, do_consent, mint, mock_state, resolve, revoke_grant

VENDOR = "mockhub-jwt"


def test_pkj_consent_refresh_and_revoke():
    tok = mint("wf-pkj")
    revoke_grant(tok, VENDOR)
    before = mock_state()["counters"]

    do_consent(tok, VENDOR)                      # code exchange via assertion
    r = resolve(tok, VENDOR)                     # 60s token -> refresh via assertion
    assert r.status_code == 200, r.text

    counters = mock_state()["counters"]
    assertions = counters["client_assertions"] - before["client_assertions"]
    assert assertions >= 2, f"expected assertion-authenticated vendor calls, saw {assertions}"
    assert counters["bad_assertions"] == before["bad_assertions"], \
        "the broker produced an assertion the vendor rejected"

    sub = "wf-pkj"
    r = requests.delete(f"{BROKER}/v1/grants/{VENDOR}/{sub}",
                        headers={"Authorization": f"Bearer {tok}"}, timeout=15)
    assert r.status_code == 200 and r.json()["revoked"] is True   # RFC 7009 via pkj
    after = mock_state()["counters"]
    assert after["revoke"] == before["revoke"] + 1
    assert after["bad_assertions"] == before["bad_assertions"]
    assert resolve(tok, VENDOR).status_code == 404


def test_pkj_and_secret_clients_are_isolated():
    """The two registry entries share the AS but not credentials: the secret
    client keeps working alongside the pkj one."""
    tok = mint("wf-pkj-iso")
    revoke_grant(tok)
    do_consent(tok)                    # plain mockhub (client_secret_post)
    assert resolve(tok).status_code == 200
    revoke_grant(tok)
