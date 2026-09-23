"""Every failure gets a deliberate status and an audit line (review change 3):
request validation (400 invalid-request), hub JWKS outage (503
hub-unavailable), authorize/callback dependency failures, the post-redeem
custody failure, delete edge cases, fixed error messages, and the
browser-response headers."""
import time

import pytest
from broker_harness import VENDOR, Harness, audit_events
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

from token_broker import sweeper
from token_broker import vendors as vendors_mod
from token_broker.coordination import CoordinationUnavailable, RedisCoordination
from token_broker.custody import CasConflict, CustodyUnavailable
from token_broker.hub import HubValidator
from token_broker.main import BROWSER_HEADERS


def assert_browser_headers(r):
    for name, value in BROWSER_HEADERS.items():
        assert r.headers.get(name) == value, name


# ------------------------------------------------------------ resolve body

@pytest.mark.parametrize("raw", [
    b"not json",
    b"[1, 2]",
    b"{}",                                                          # no vendor
    b'{"vendor": 7}',
    b'{"vendor": ""}',
    b'{"vendor": "mockhub", "sub": 5}',
    b'{"vendor": "mockhub", "min_ttl_s": "soon"}',
    b'{"vendor": "mockhub", "min_ttl_s": true}',
    b'{"vendor": "mockhub", "min_ttl_s": -1}',
    b'{"vendor": "mockhub", "min_ttl_s": 30.5}',
    b'{"vendor": "mockhub", "required_scopes": "issues:read"}',
    b'{"vendor": "mockhub", "required_scopes": ["issues:read", 7]}',
])
async def test_malformed_resolve_body_is_400(raw, capsys):
    h = Harness()
    async with h.client() as c:
        r = await c.post("/v1/tokens/resolve", content=raw,
                         headers={"Authorization": f"Bearer {h.token()}",
                                  "content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["title"] == "invalid-request"
    deny = audit_events(capsys, "broker.resolve")
    assert deny and deny[0]["reason"] == "invalid_request"


async def test_well_formed_body_still_resolves():
    h = Harness()
    h.put()
    r = await h.resolve(min_ttl_s=0, required_scopes=["issues:read"], sub="wf-user-1")
    assert r.status_code == 200


# ------------------------------------------------------------ hub JWKS outage

class _JWKSDown:
    def get_signing_key_from_jwt(self, token):
        raise PyJWKClientConnectionError(
            'Fail to fetch data from the url, err: "http://hub.internal/jwks"')


class _UnknownKid:
    def get_signing_key_from_jwt(self, token):
        raise PyJWKClientError('Unable to find a signing key that matches: "k9"')


async def _call(h, method, path, **kw):
    async with h.client() as c:
        return await c.request(method, path,
                               headers={"Authorization": f"Bearer {h.token()}"}, **kw)


ROUTES = [("POST", "/v1/tokens/resolve", {"json": {"vendor": VENDOR}}),
          ("GET", "/v1/grants", {}),
          ("DELETE", f"/v1/grants/{VENDOR}/wf-user-1", {}),
          ("GET", f"/v1/admin/vendors/{VENDOR}", {})]


@pytest.mark.parametrize("method,path,kw", ROUTES)
async def test_jwks_outage_is_503_hub_unavailable(method, path, kw):
    h = Harness()
    h.broker.hub = HubValidator(h.cfg, jwks_client=_JWKSDown())
    r = await _call(h, method, path, **kw)
    assert r.status_code == 503
    assert r.json()["title"] == "hub-unavailable"
    assert "hub.internal" not in r.text


@pytest.mark.parametrize("method,path,kw", ROUTES)
async def test_unknown_signing_key_is_still_401(method, path, kw):
    h = Harness()
    h.broker.hub = HubValidator(h.cfg, jwks_client=_UnknownKid())
    r = await _call(h, method, path, **kw)
    assert r.status_code == 401 and r.json()["title"] == "invalid-hub-token"


# ------------------------------------------------------------ authorize

async def _authorize(h):
    txn = await h.broker.new_txn("wf-user-1", VENDOR, ["issues:read"])
    async with h.client() as c:
        return await c.get(f"/v1/authorize/{VENDOR}", params={"txn": txn})


@pytest.mark.parametrize("setup,title,reason", [
    (lambda v: setattr(v, "endpoints_error",
                       vendors_mod.VendorUnavailable("mockhub metadata unavailable")),
     "vendor-unavailable", "vendor_unavailable"),
    (lambda v: setattr(v, "client_error", CustodyUnavailable()),
     "vault-unavailable", "vault_unavailable"),
    (lambda v: setattr(v, "client", None), "vendor-unavailable", "no_client_credential"),
])
async def test_authorize_dependency_failures(setup, title, reason, capsys):
    h = Harness()
    setup(h.vendors)
    r = await _authorize(h)
    assert r.status_code == 503 and r.json()["title"] == title
    fails = audit_events(capsys, "broker.consent.fail")
    assert fails and fails[0]["reason"] == reason


async def test_authorize_redirect_carries_browser_headers():
    r = await _authorize(Harness())
    assert r.status_code in (302, 307)
    assert_browser_headers(r)


# ------------------------------------------------------------ callback

async def test_callback_success_writes_grant_with_browser_headers():
    h = Harness()
    r = await h.callback(await h.start_consent())
    assert r.status_code == 200 and "Connected" in r.text
    assert_browser_headers(r)
    assert h.stored()["access_token"] == "at-consent"


async def test_callback_error_pages_carry_browser_headers():
    h = Harness()
    r = await h.callback("never-issued")
    assert r.status_code == 400
    assert_browser_headers(r)


async def test_exchange_failure_is_502():
    h = Harness()
    state = await h.start_consent()
    h.vendors.exchange_error = vendors_mod.VendorUnavailable("vendor token endpoint 503")
    r = await h.callback(state)
    assert r.status_code == 502
    assert_browser_headers(r)


async def test_custody_failure_after_redeem_revokes_the_new_grant(capsys):
    """Review M1: the code is spent, custody refuses the write. The fresh
    grant is revoked at the vendor instead of being orphaned there."""
    h = Harness()
    state = await h.start_consent()
    h.custody.fail = True
    r = await h.callback(state)
    assert r.status_code == 503
    assert [e["refresh_token"] for e in h.vendors.revoked] == ["rt-consent"]
    fails = audit_events(capsys, "broker.consent.fail")
    assert fails[-1]["reason"] == "post_redeem_failure"
    assert fails[-1]["new_grant_revoked"] is True
    assert "at-consent" not in str(fails) and "rt-consent" not in str(fails)


async def test_post_redeem_revoke_failure_is_audited(capsys):
    h = Harness()
    state = await h.start_consent()
    h.custody.fail = True
    h.vendors.revoke_error = vendors_mod.VendorUnavailable("vendor revocation unavailable")
    r = await h.callback(state)
    assert r.status_code == 503
    assert audit_events(capsys, "broker.consent.fail")[-1]["new_grant_revoked"] is False


# ------------------------------------------------------------ delete

async def _delete(h):
    return await _call(h, "DELETE", f"/v1/grants/{VENDOR}/wf-user-1")


async def test_delete_without_vendor_revocation_deletes_locally(capsys):
    h = Harness()
    h.put()
    h.vendors.revoke_error = vendors_mod.RevocationUnsupported("no revocation endpoint")
    r = await _delete(h)
    assert r.status_code == 200
    assert r.json() == {"revoked": True, "vendor_revocation": "unsupported"}
    assert h.stored() is None
    assert audit_events(capsys, "broker.revoke")[-1]["outcome"] == "unsupported"


async def test_delete_parks_the_newer_pair_when_a_refresh_races():
    """Review: CasConflict while parking REVOKE_PENDING used to be a 500."""
    h = Harness()
    h.put()
    h.vendors.revoke_error = vendors_mod.VendorUnavailable("vendor revocation unavailable")
    h.vendors.on_revoke = lambda: h.put(refresh_token="rt-newer", refresh_generation=2)
    r = await _delete(h)
    assert r.status_code == 502 and r.json()["title"] == "revoke-pending"
    stored = h.stored()
    assert stored["state"] == "REVOKE_PENDING"
    assert stored["refresh_token"] == "rt-newer"


async def test_delete_gives_up_with_503_when_the_grant_keeps_moving(monkeypatch):
    h = Harness()
    h.put()
    h.vendors.revoke_error = vendors_mod.VendorUnavailable("vendor revocation unavailable")

    def always_conflict(vendor, sub, entry, cas=None):
        raise CasConflict("check-and-set parameter did not match")

    monkeypatch.setattr(h.custody, "write", always_conflict)
    r = await _delete(h)
    assert r.status_code == 503 and r.json()["title"] == "vendor-unavailable"


async def test_sweeper_drains_parked_grant_without_vendor_revocation(capsys):
    h = Harness()
    h.put(state="REVOKE_PENDING")
    h.vendors.revoke_error = vendors_mod.RevocationUnsupported("no revocation endpoint")
    await sweeper.sweep_entry(h.broker, VENDOR, "wf-user-1")
    assert h.stored() is None
    revoke = audit_events(capsys, "broker.revoke")
    assert revoke[-1]["outcome"] == "unsupported" and revoke[-1]["path"] == "sweep-retry"


# ------------------------------------------------------------ fixed messages (L9)

async def test_custody_outage_detail_is_fixed():
    h = Harness()
    h.custody.fail = True
    r = await h.resolve()
    assert r.status_code == 503
    assert r.json()["detail"] == "custody backend unavailable"


def test_custody_error_message_hides_backend_text():
    exc = CustodyUnavailable(ConnectionError("HTTPConnectionPool(host='vault.internal')"))
    assert str(exc) == "custody backend unavailable"


async def test_coordination_error_message_hides_redis_address():
    from redis.exceptions import ConnectionError as RedisConnectionError

    class DownRedis:
        def register_script(self, _):
            return None

        async def set(self, *a, **kw):
            raise RedisConnectionError("Error connecting to redis.internal:6379")

    coord = RedisCoordination(Harness().cfg, "inst", client=DownRedis())
    with pytest.raises(CoordinationUnavailable) as exc:
        await coord.try_refresh_lock(VENDOR, "wf-user-1")
    assert str(exc.value) == "coordination store unavailable"


async def test_redis_outage_on_refresh_path_is_fixed_503():
    """End to end on the refresh path: a redis error surfaces as 503
    coordination-unavailable with the fixed detail."""
    h = Harness(coord="redis")
    h.put(expires_at=time.time() + 100)

    async def down(*a, **kw):
        from redis.exceptions import ConnectionError as RedisConnectionError
        raise RedisConnectionError("Error connecting to redis.internal:6379")

    h.coord._r.set = down
    r = await h.resolve()
    assert r.status_code == 503
    assert r.json()["title"] == "coordination-unavailable"
    assert r.json()["detail"] == "coordination store unavailable"
