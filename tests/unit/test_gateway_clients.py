"""The gateway's broker client over HTTP (httpx mock transport): how grant
listing and disconnect answers map to outcomes and errors."""

import httpx
import jwt
import pytest
from gateway_helpers import make_gateway_config

from mcp_gateway.clients import Broker, HandoffError, Unavailable

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
