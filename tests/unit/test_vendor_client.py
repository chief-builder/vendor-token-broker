"""VendorClient failure classification (design §9) against an in-process
httpx.MockTransport: every metadata/token/userinfo/revocation failure is a
VendorError the routes handle (never a raw httpx or JSON exception), messages
carry no vendor hostnames, and the vendor-user lookup is best-effort."""

import json
import time

import httpx
import pytest
from broker_harness import Harness
from unit_helpers import REPO_ROOT, MemoryCustody, make_config

from token_broker import vendors as vendors_mod
from token_broker.vendors import (
    InvalidGrant,
    RevocationUnsupported,
    VendorClient,
    VendorUnavailable,
)

META_URL = "http://mock-vendor:8310/.well-known/oauth-authorization-server"
TOKEN_URL = "http://mock-vendor:8310/token"
USER_URL = "http://mock-vendor:8310/user"
REVOKE_URL = "http://mock-vendor:8310/revoke"
META = {
    "issuer": "http://mock-vendor:8310",
    "authorization_endpoint": "http://localhost:8310/authorize",
    "token_endpoint": TOKEN_URL,
    "revocation_endpoint": REVOKE_URL,
    "userinfo_endpoint": USER_URL,
}
DOWN = object()  # route value: raise a connection error


def json_response(body, status=200):
    return httpx.Response(status, json=body)


@pytest.fixture
def vendor_http(monkeypatch):
    """Route table {url: Response | DOWN | callable}; unrouted URLs are DOWN."""
    routes = {META_URL: json_response(META)}
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        found = routes.get(str(request.url), DOWN)
        if found is DOWN:
            raise httpx.ConnectError(f"connect to {request.url.host} refused", request=request)
        return found(request) if callable(found) else found

    def client_factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real(transport=httpx.MockTransport(handler), timeout=5)

    monkeypatch.setattr(vendors_mod.httpx, "AsyncClient", client_factory)
    return routes


@pytest.fixture
def client():
    custody = MemoryCustody()
    custody.clients["mockhub"] = {"client_id": "cid", "client_secret": "secret"}
    return VendorClient(make_config(), custody)


# ------------------------------------------------------------ RFC 8414 metadata


@pytest.mark.parametrize(
    "meta",
    [
        DOWN,
        httpx.Response(500),
        httpx.Response(200, text="<html>", headers={"content-type": "text/html"}),
        json_response(["not", "an", "object"]),
        json_response({"issuer": "x", "authorization_endpoint": "http://a"}),  # no token_endpoint
    ],
)
async def test_metadata_failures_are_vendor_unavailable(client, vendor_http, meta):
    vendor_http[META_URL] = meta
    with pytest.raises(VendorUnavailable) as exc:
        await client.endpoints("mockhub")
    assert "mock-vendor" not in str(exc.value)


async def test_metadata_is_cached_after_success(client, vendor_http):
    assert (await client.endpoints("mockhub"))["token_endpoint"] == TOKEN_URL
    vendor_http[META_URL] = DOWN
    assert (await client.endpoints("mockhub"))["token_endpoint"] == TOKEN_URL


# ------------------------------------------------------------ token endpoint


@pytest.mark.parametrize(
    "response",
    [
        DOWN,
        httpx.Response(502),
        httpx.Response(200, text="{not json", headers={"content-type": "application/json"}),
        json_response(["a", "list"]),
        json_response({"token_type": "bearer"}),  # 200 without access_token
        json_response({"error": "invalid_client"}, status=401),
    ],
)
async def test_token_endpoint_failures_are_vendor_unavailable(client, vendor_http, response):
    vendor_http[TOKEN_URL] = response
    with pytest.raises(VendorUnavailable) as exc:
        await client.refresh("mockhub", "rt-0")
    assert "mock-vendor" not in str(exc.value)


@pytest.mark.parametrize(
    "response",
    [
        json_response({"error": "invalid_grant"}, status=400),  # RFC-shaped
        json_response({"error": "bad_refresh_token"}),  # GitHub: 200 + error
    ],
)
async def test_rejected_grant_is_invalid_grant(client, vendor_http, response):
    vendor_http[TOKEN_URL] = response
    with pytest.raises(InvalidGrant):
        await client.refresh("mockhub", "rt-0")


async def test_token_success_returns_body(client, vendor_http):
    vendor_http[TOKEN_URL] = json_response({"access_token": "at-1", "expires_in": 60})
    assert (await client.refresh("mockhub", "rt-0"))["access_token"] == "at-1"


# ------------------------------------------------------------ vendor user id


@pytest.mark.parametrize(
    "response",
    [
        DOWN,
        httpx.Response(401),
        httpx.Response(200, text="{not json", headers={"content-type": "application/json"}),
        json_response(["a", "list"]),
    ],
)
async def test_vendor_user_id_is_best_effort(client, vendor_http, response):
    vendor_http[USER_URL] = response
    assert await client.vendor_user_id("mockhub", "at") == "unknown"


async def test_vendor_user_id_metadata_failure_is_unknown(client, vendor_http):
    vendor_http[META_URL] = DOWN
    assert await client.vendor_user_id("mockhub", "at") == "unknown"


async def test_vendor_user_id_happy_path(client, vendor_http):
    vendor_http[USER_URL] = json_response({"id": 4217, "login": "octocat"})
    assert await client.vendor_user_id("mockhub", "at") == "4217"


# ------------------------------------------------------------ revocation


async def test_no_revocation_endpoint_is_unsupported(client, vendor_http):
    vendor_http[META_URL] = json_response(
        {k: v for k, v in META.items() if k != "revocation_endpoint"}
    )
    with pytest.raises(RevocationUnsupported):
        await client.revoke("mockhub", {"refresh_token": "rt", "access_token": "at"})


async def test_revocation_transport_error_is_unavailable(client, vendor_http):
    with pytest.raises(VendorUnavailable) as exc:
        await client.revoke("mockhub", {"refresh_token": "rt", "access_token": "at"})
    assert "mock-vendor" not in str(exc.value)


async def test_revocation_success(client, vendor_http):
    vendor_http[REVOKE_URL] = json_response({})
    await client.revoke("mockhub", {"refresh_token": "rt", "access_token": "at"})


# ------------------------------------------------------------ end to end


async def test_metadata_outage_during_refresh_restores_active(vendor_http):
    """Review M2: a metadata failure mid-refresh used to escape as a raw
    httpx error (500) and strand a persisted REFRESHING marker. It is now
    VendorDown: 503 vendor-unavailable and the entry is ACTIVE again."""
    vendor_http[META_URL] = DOWN
    h = Harness(coord="redis")
    h.broker.vendors = VendorClient(h.cfg, h.custody)
    h.put(expires_at=time.time() + 100)
    r = await h.resolve()
    assert r.status_code == 503 and r.json()["title"] == "vendor-unavailable"
    assert h.stored()["state"] == "ACTIVE"


# ------------------------------------------------ RFC 8707 resource indicator


def _capture_token_forms(vendor_http) -> list[dict]:
    forms: list[dict] = []

    def token(request: httpx.Request) -> httpx.Response:
        forms.append(dict(httpx.QueryParams(request.content.decode())))
        return json_response({"access_token": "at", "refresh_token": "rt", "expires_in": 60})

    vendor_http[TOKEN_URL] = token
    return forms


async def test_code_exchange_and_refresh_send_the_vendors_resource(client, vendor_http):
    mcp = "https://mcp.example.test/mcp"
    client._registry["mockhub"] = {**client._registry["mockhub"], "resource": mcp}
    forms = _capture_token_forms(vendor_http)
    await client.exchange_code("mockhub", "code", "verifier", "http://broker/cb")
    await client.refresh("mockhub", "rt-0")
    assert [f["grant_type"] for f in forms] == ["authorization_code", "refresh_token"]
    assert all(f["resource"] == mcp for f in forms)


async def test_resource_can_be_left_off_refresh(client, vendor_http):
    """Linear refuses `resource` on refresh (and keeps the token bound)."""
    mcp = "https://mcp.example.test/mcp/readonly"
    client._registry["mockhub"] = {
        **client._registry["mockhub"],
        "resource": mcp,
        "resource_on_refresh": False,
    }
    forms = _capture_token_forms(vendor_http)
    await client.exchange_code("mockhub", "code", "verifier", "http://broker/cb")
    await client.refresh("mockhub", "rt-0")
    assert forms[0]["resource"] == mcp and "resource" not in forms[1]


async def test_ordinary_vendor_token_requests_have_no_resource(client, vendor_http):
    forms = _capture_token_forms(vendor_http)
    await client.exchange_code("mockhub", "code", "verifier", "http://broker/cb")
    await client.refresh("mockhub", "rt-0")
    assert len(forms) == 2 and all("resource" not in f for f in forms)


# ------------------------------------------------- GitHub grant-deletion revoke

GITHUB_GRANT = "https://api.github.com/applications/gh-cid/grant"


@pytest.fixture
def github_client():
    custody = MemoryCustody()
    custody.clients["github"] = {"client_id": "gh-cid", "client_secret": "gh-secret"}
    return VendorClient(make_config(), custody)


@pytest.mark.parametrize("status", [204, 404, 422])
async def test_github_grant_revoke_accepts_done_or_gone(github_client, vendor_http, status):
    seen = []

    def grant(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    vendor_http[GITHUB_GRANT] = grant
    await github_client.revoke("github", {"access_token": "gho_live"})
    (req,) = seen
    assert req.method == "DELETE"
    assert req.headers["authorization"].startswith("Basic ")  # client_id:secret
    assert b"gho_live" in req.content


@pytest.mark.parametrize("status", [401, 500])
async def test_github_grant_revoke_failure_is_vendor_unavailable(
    github_client, vendor_http, status
):
    vendor_http[GITHUB_GRANT] = httpx.Response(status)
    with pytest.raises(VendorUnavailable) as err:
        await github_client.revoke("github", {"access_token": "gho_live"})
    assert "gho_live" not in str(err.value)


async def test_github_grant_revoke_outage_is_vendor_unavailable(github_client, vendor_http):
    with pytest.raises(VendorUnavailable):  # GITHUB_GRANT unrouted: connection refused
        await github_client.revoke("github", {"access_token": "gho_live"})


async def test_github_grant_revoke_skips_a_scrubbed_entry(github_client, vendor_http):
    await github_client.revoke("github", {"access_token": ""})  # unrouted: any call would fail


async def test_github_grant_url_is_configurable(tmp_path, vendor_http):
    registry = json.loads((REPO_ROOT / "registry.example.json").read_text())
    ghes = "https://ghe.example.com/api/v3/applications/{client_id}/grant"
    registry["github"]["revocation"]["grant_url"] = ghes
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry))
    custody = MemoryCustody()
    custody.clients["github"] = {"client_id": "gh-cid", "client_secret": "gh-secret"}
    vendor_http[ghes.format(client_id="gh-cid")] = httpx.Response(204)
    await VendorClient(make_config(registry_path=path), custody).revoke(
        "github", {"access_token": "gho_live"}
    )
