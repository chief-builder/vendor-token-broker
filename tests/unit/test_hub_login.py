"""HubLogin against an in-process mock hub (httpx.MockTransport) that signs
real ID tokens: discovery, the authorization URL, the code exchange, and
every ID-token check (signature, issuer, audience, expiry, nonce)."""
import time

import httpx
import jwt
import pytest
from unit_helpers import StaticJWKS, make_config

from token_broker import hub_login as hub_login_mod
from token_broker.hub import HubUnavailable, HubValidator
from token_broker.hub_login import HubLogin, HubLoginError

ISSUER = "https://hub.test/realms/mcp-plane"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
TOKEN = f"{ISSUER}/token"
META = {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth", "token_endpoint": TOKEN}
DOWN = object()


@pytest.fixture
def hub(monkeypatch, rsa_key):
    """{url: Response | DOWN | callable}; `posted` records token-endpoint forms."""
    routes = {DISCOVERY: httpx.Response(200, json=META)}
    posted = []
    real = httpx.AsyncClient

    def handler(request):
        if request.method == "POST":
            posted.append(dict(httpx.QueryParams(request.content.decode())))
        found = routes.get(str(request.url), DOWN)
        if found is DOWN:
            raise httpx.ConnectError("refused", request=request)
        return found(request) if callable(found) else found

    monkeypatch.setattr(hub_login_mod.httpx, "AsyncClient",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    return routes, posted


def id_token(key, **overrides):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": "wf-user-1", "aud": "vtb-broker",
              "exp": now + 300, "iat": now, "nonce": "n-1"}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="PS256")


def login(rsa_key, **cfg):
    config = make_config(hub_issuer=ISSUER, **cfg)
    return HubLogin(config, HubValidator(config, jwks_client=StaticJWKS(rsa_key.public_key())))


async def _exchange(login_client, nonce="n-1"):
    return await login_client.exchange(code="c", verifier="v",
                                       redirect_uri="https://b/v1/callback/_hub", nonce=nonce)


async def test_authorization_url_carries_oidc_pkce_and_hint(hub, rsa_key):
    url = await login(rsa_key).authorization_url(
        state="s", nonce="n", challenge="ch", login_hint="wf-user-1",
        redirect_uri="https://b/v1/callback/_hub")
    q = dict(httpx.URL(url).params)
    assert url.startswith(f"{ISSUER}/auth?")
    assert q == {"client_id": "vtb-broker", "response_type": "code", "scope": "openid",
                 "redirect_uri": "https://b/v1/callback/_hub", "state": "s", "nonce": "n",
                 "code_challenge": "ch", "code_challenge_method": "S256",
                 "login_hint": "wf-user-1"}


async def test_authorization_url_omits_an_absent_hint(hub, rsa_key):
    url = await login(rsa_key).authorization_url(
        state="s", nonce="n", challenge="ch", login_hint=None,
        redirect_uri="https://b/v1/callback/_hub")
    assert "login_hint" not in dict(httpx.URL(url).params)


async def test_valid_id_token_returns_claims(hub, rsa_key):
    routes, posted = hub
    routes[TOKEN] = httpx.Response(200, json={"id_token": id_token(rsa_key)})
    claims = await _exchange(login(rsa_key))
    assert claims["sub"] == "wf-user-1"
    assert posted[0]["grant_type"] == "authorization_code"
    assert posted[0]["code_verifier"] == "v" and "client_secret" not in posted[0]


async def test_confidential_client_sends_its_secret(hub, rsa_key):
    routes, posted = hub
    routes[TOKEN] = httpx.Response(200, json={"id_token": id_token(rsa_key)})
    await _exchange(login(rsa_key, hub_login_client_secret="s3cret"))
    assert posted[0]["client_secret"] == "s3cret"


@pytest.mark.parametrize("overrides", [
    {"nonce": "someone-elses-nonce"},
    {"aud": "another-client"},
    {"iss": "https://evil.example"},
    {"exp": int(time.time()) - 120, "iat": int(time.time()) - 600},
    {"nonce": None},
])
async def test_bad_id_tokens_are_rejected(hub, rsa_key, overrides):
    routes, _ = hub
    routes[TOKEN] = httpx.Response(200, json={"id_token": id_token(rsa_key, **overrides)})
    with pytest.raises(HubLoginError):
        await _exchange(login(rsa_key))


async def test_id_token_signed_by_another_key_is_rejected(hub, rsa_key, ec_key):
    from cryptography.hazmat.primitives.asymmetric import rsa
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    routes, _ = hub
    routes[TOKEN] = httpx.Response(200, json={"id_token": id_token(other)})
    with pytest.raises(HubLoginError):
        await _exchange(login(rsa_key))


@pytest.mark.parametrize("response,error", [
    (httpx.Response(400, json={"error": "invalid_grant"}), HubLoginError),
    (httpx.Response(200, json={"access_token": "no id token"}), HubLoginError),
    (httpx.Response(200, text="<html>"), HubLoginError),
    (httpx.Response(503), HubUnavailable),
    (DOWN, HubUnavailable),
])
async def test_token_endpoint_failures(hub, rsa_key, response, error):
    routes, _ = hub
    routes[TOKEN] = response
    with pytest.raises(error):
        await _exchange(login(rsa_key))


@pytest.mark.parametrize("discovery,error", [
    (DOWN, HubUnavailable),
    (httpx.Response(200, json={**META, "issuer": "https://other.example"}), HubLoginError),
    (httpx.Response(200, json={"issuer": ISSUER}), HubLoginError),
])
async def test_discovery_failures(hub, rsa_key, discovery, error):
    routes, _ = hub
    routes[DISCOVERY] = discovery
    with pytest.raises(error):
        await login(rsa_key).check()


INTERNAL = "http://hub.internal:8080/realms/mcp-plane/.well-known/openid-configuration"


async def test_discovery_can_come_from_an_internal_url(hub, rsa_key):
    """Public issuer, internal address: the document is fetched from
    HUB_DISCOVERY_URL and must still name the public issuer."""
    routes, _ = hub
    del routes[DISCOVERY]
    routes[INTERNAL] = httpx.Response(200, json=META)
    url = await login(rsa_key, hub_discovery_url=INTERNAL).authorization_url(
        state="s", nonce="n", challenge="ch", login_hint=None,
        redirect_uri="https://b/v1/callback/_hub")
    assert url.startswith(f"{ISSUER}/auth?")


async def test_internal_discovery_must_name_the_configured_issuer(hub, rsa_key):
    routes, _ = hub
    routes[INTERNAL] = httpx.Response(200, json={**META, "issuer": "http://hub.internal:8080"})
    with pytest.raises(HubLoginError):
        await login(rsa_key, hub_discovery_url=INTERNAL).check()
