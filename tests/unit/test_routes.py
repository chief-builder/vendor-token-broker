"""Route-audit-as-unit-test (design §10): the broker serves exactly the
7-route no-issuance surface. Audits the router itself (app.routes), not the
OpenAPI document, which omits framework routes such as /docs. Runs offline
on every push."""
from broker_harness import Harness

from token_broker.main import create_app

FROZEN_ROUTES = {
    "/healthz",
    "/v1/tokens/resolve",
    "/v1/authorize/{vendor}",
    "/v1/callback/{vendor}",
    "/v1/grants/{vendor}/{sub}",
    "/v1/grants",
    "/v1/admin/vendors/{vendor}",
}


def served_paths(app) -> set[str]:
    return {route.path for route in app.routes}


def test_route_table_is_exactly_the_seven_frozen_paths(cfg):
    assert served_paths(create_app(cfg)) == FROZEN_ROUTES


def test_no_issuance_surface(cfg):
    """No token minting, no JWKS, no OIDC discovery — custodian, not issuer."""
    for p in served_paths(create_app(cfg)):
        assert "jwks" not in p
        assert not p.endswith("/token")
        assert "well-known" not in p
        assert "issue" not in p


async def test_framework_doc_routes_are_not_served():
    async with Harness().client() as c:
        for path in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
            assert (await c.get(path)).status_code == 404, path
