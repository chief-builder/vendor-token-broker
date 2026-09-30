"""The gateway's broker client over HTTP (httpx mock transport): how grant
listing and disconnect answers map to outcomes and errors."""

import json
from urllib.parse import parse_qsl

import httpx
import jwt
import pytest
from gateway_helpers import make_gateway_config

from mcp_gateway.clients import Broker, HandoffError, Hub, Unavailable

HUB_JWT = jwt.encode({"sub": "tenant/alice"}, "k" * 32, algorithm="HS256")


def broker(handler) -> tuple[Broker, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return Broker(make_gateway_config(), http), seen


def problem(status: int, title: str) -> httpx.Response:
    return httpx.Response(
        status, json={"title": title}, headers={"content-type": "application/problem+json"}
    )


async def test_connected_reads_the_grant_list():
    b, _ = broker(
        lambda r: httpx.Response(
            200,
            json={
                "grants": [
                    {"vendor": "github", "state": "ACTIVE"},
                    {"vendor": "linear", "state": "STALE"},
                ]
            },
        )
    )
    assert await b.connected(HUB_JWT, "github") is True
    assert await b.connected(HUB_JWT, "linear") is False


@pytest.mark.parametrize(
    "response, error",
    [
        (problem(503, "vault-unavailable"), Unavailable),
        (problem(401, "invalid-hub-token"), HandoffError),
    ],
)
async def test_connected_never_turns_a_failure_into_not_connected(response, error):
    b, _ = broker(lambda r: response)
    with pytest.raises(error):
        await b.connected(HUB_JWT, "github")


@pytest.mark.parametrize(
    "response, outcome",
    [
        (httpx.Response(200, json={"revoked": True}), "revoked"),
        (
            httpx.Response(200, json={"revoked": True, "vendor_revocation": "unsupported"}),
            "unsupported",
        ),
        (problem(404, "no-grant"), "not-connected"),
        (problem(502, "revoke-pending"), "pending"),
    ],
)
async def test_disconnect_outcomes(response, outcome):
    b, seen = broker(lambda r: response)
    assert await b.disconnect(HUB_JWT, "github") == outcome
    [request] = seen
    assert request.method == "DELETE"
    assert request.url.raw_path == b"/v1/grants/github/tenant/alice"  # the hub JWT's sub
    assert request.headers["authorization"] == f"Bearer {HUB_JWT}"


@pytest.mark.parametrize(
    "response, error",
    [
        (problem(503, "vault-unavailable"), Unavailable),
        (problem(503, "coordination-unavailable"), Unavailable),
        (problem(401, "invalid-hub-token"), HandoffError),
        (problem(403, "forbidden"), HandoffError),
        (problem(404, "unknown-vendor"), HandoffError),
    ],
)
async def test_disconnect_failures(response, error):
    b, _ = broker(lambda r: response)
    with pytest.raises(error):
        await b.disconnect(HUB_JWT, "github")


async def test_unreachable_broker_is_unavailable():
    def down(request):
        raise httpx.ConnectError("refused")

    b, _ = broker(down)
    with pytest.raises(Unavailable):
        await b.disconnect(HUB_JWT, "github")
    with pytest.raises(Unavailable):
        await b.connected(HUB_JWT, "github")


# ------------------------------------------- hub token exchange (RFC 8693)

MCP_TOKEN = "mcp-access-token"


def hub(handler) -> tuple[Hub, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return Hub(make_gateway_config(), http), seen


async def test_exchange_sends_an_rfc8693_request_and_returns_the_hub_jwt():
    h, seen = hub(lambda r: httpx.Response(200, json={"access_token": HUB_JWT}))
    assert await h.exchange(MCP_TOKEN) == HUB_JWT
    (req,) = seen
    form = dict(parse_qsl(req.content.decode()))
    cfg = make_gateway_config()
    assert str(req.url) == cfg.hub_token_endpoint
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert form["subject_token"] == MCP_TOKEN
    assert form["subject_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert form["requested_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert form["scope"] == cfg.exchange_scope
    assert form["client_id"] == cfg.client_id and form["client_secret"] == cfg.client_secret


@pytest.mark.parametrize(
    "response, error",
    [
        (httpx.Response(400, json={"error": "invalid_grant"}), HandoffError),
        (httpx.Response(401, json={"error": "invalid_client"}), HandoffError),
        (httpx.Response(403, text="<html>no</html>"), HandoffError),
        (httpx.Response(200, json={"token_type": "Bearer"}), HandoffError),  # no token
        (httpx.Response(200, text="not json"), HandoffError),
        (httpx.Response(200, json=["a", "list"]), HandoffError),
        (httpx.Response(500), Unavailable),
        (httpx.Response(503, json={"error": "temporarily_unavailable"}), Unavailable),
    ],
)
async def test_exchange_failures_map_to_handoff_or_unavailable(response, error):
    h, _ = hub(lambda r: response)
    with pytest.raises(error) as err:
        await h.exchange(MCP_TOKEN)
    assert MCP_TOKEN not in str(err.value)


async def test_exchange_refusal_names_the_oauth_error():
    h, _ = hub(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(HandoffError, match="400 invalid_grant"):
        await h.exchange(MCP_TOKEN)


async def test_exchange_outage_is_unavailable():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    h, _ = hub(down)
    with pytest.raises(Unavailable):
        await h.exchange(MCP_TOKEN)


# --------------------------------------------------------------- broker resolve


async def test_resolve_sends_the_hub_jwt_and_no_scopes():
    b, seen = broker(lambda r: httpx.Response(200, json={"access_token": "vendor-at"}))
    resolved = await b.resolve(HUB_JWT, "github")
    assert resolved.token == "vendor-at"
    (req,) = seen
    assert req.headers["authorization"] == f"Bearer {HUB_JWT}"
    body = json.loads(req.content)
    assert body == {"vendor": "github", "min_ttl_s": make_gateway_config().min_ttl_s}


def consent(status: int, title: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"title": title, "authorize_uri": "http://broker/v1/authorize/github?txn=t"},
        headers={"content-type": "application/problem+json"},
    )


@pytest.mark.parametrize(
    "response",
    [consent(404, "needs-consent"), consent(409, "needs-reconsent-scope")],
)
async def test_resolve_consent_answers_carry_the_link(response):
    b, _ = broker(lambda r: response)
    resolved = await b.resolve(HUB_JWT, "github")
    assert resolved.token is None
    assert resolved.consent_url == "http://broker/v1/authorize/github?txn=t"
    assert resolved.problem == response.json()["title"]


@pytest.mark.parametrize(
    "response, title",
    [
        (problem(409, "revoke-pending"), "revoke-pending"),
        (problem(404, "unknown-vendor"), "unknown-vendor"),
        (problem(403, "scope-exceeds-ceiling"), "scope-exceeds-ceiling"),
        (httpx.Response(400, text="bad"), "400"),
    ],
)
async def test_resolve_other_refusals_are_problems_without_a_link(response, title):
    b, _ = broker(lambda r: response)
    resolved = await b.resolve(HUB_JWT, "github")
    assert resolved.token is None and resolved.consent_url is None
    assert resolved.problem == title


@pytest.mark.parametrize(
    "response, error",
    [
        (problem(401, "invalid-hub-token"), HandoffError),
        (problem(503, "vault-unavailable"), Unavailable),
        (problem(503, "coordination-unavailable"), Unavailable),
        (httpx.Response(502, text="<html>bad gateway</html>"), Unavailable),
        (httpx.Response(200, json={"expires_at": 1}), Unavailable),  # no token
        (httpx.Response(200, text="not json"), Unavailable),
    ],
)
async def test_resolve_failures_never_look_like_consent(response, error):
    b, _ = broker(lambda r: response)
    with pytest.raises(error):
        await b.resolve(HUB_JWT, "github")


async def test_resolve_outage_is_unavailable():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    b, _ = broker(down)
    with pytest.raises(Unavailable):
        await b.resolve(HUB_JWT, "github")


@pytest.mark.parametrize(
    "response", [httpx.Response(200, json={"nope": 1}), httpx.Response(200, text="x")]
)
async def test_malformed_grant_list_is_unavailable_not_disconnected(response):
    b, _ = broker(lambda r: response)
    with pytest.raises(Unavailable):
        await b.connected(HUB_JWT, "github")
