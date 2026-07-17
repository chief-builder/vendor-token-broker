"""Route-audit-as-unit-test (design §11, EG-04): the broker exposes exactly
the 7-route no-issuance surface. Runs offline on every push."""
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


def test_route_table_is_exactly_the_seven_frozen_paths(cfg):
    paths = set(create_app(cfg).openapi()["paths"])
    assert paths == FROZEN_ROUTES


def test_no_issuance_surface(cfg):
    """No token minting, no JWKS, no OIDC discovery — custodian, not issuer."""
    paths = set(create_app(cfg).openapi()["paths"])
    for p in paths:
        assert "jwks" not in p
        assert not p.endswith("/token")
        assert "well-known" not in p
        assert "issue" not in p
