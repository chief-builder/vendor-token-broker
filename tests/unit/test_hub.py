"""Hub-JWT validation matrix (design §3): algorithm pinning, issuer,
tier-audience, contract shape, required claims. Keys are generated locally;
the JWKS client is faked — no network."""
import time

import pytest
from unit_helpers import StaticJWKS, make_config, mint_hub_token

from token_broker.hub import HubAuthError, HubValidator


@pytest.fixture
def validator_rsa(cfg, rsa_key):
    return HubValidator(cfg, jwks_client=StaticJWKS(rsa_key.public_key()))


@pytest.fixture
def validator_ec(cfg, ec_key):
    return HubValidator(cfg, jwks_client=StaticJWKS(ec_key.public_key()))


def bearer(token: str) -> str:
    return f"Bearer {token}"


def test_valid_ps256_token_accepted(cfg, rsa_key, validator_rsa):
    claims = validator_rsa.validate(bearer(mint_hub_token(rsa_key, "PS256", cfg)))
    assert claims["sub"] == "wf-user-1"


def test_valid_es256_token_accepted(cfg, ec_key, validator_ec):
    claims = validator_ec.validate(bearer(mint_hub_token(ec_key, "ES256", cfg)))
    assert claims["sub"] == "wf-user-1"


def test_rs256_rejected_even_with_resolvable_key(cfg, rsa_key, validator_rsa):
    """RS256 is forbidden by the contract pin — same key, wrong alg."""
    token = mint_hub_token(rsa_key, "RS256", cfg)
    with pytest.raises(HubAuthError):
        validator_rsa.validate(bearer(token))


def test_hmac_rejected(cfg, validator_rsa):
    import jwt as pyjwt
    token = pyjwt.encode({"iss": cfg.hub_issuer, "sub": "x"}, "secret", algorithm="HS256")
    with pytest.raises(HubAuthError):
        validator_rsa.validate(bearer(token))


def test_wrong_issuer_rejected(cfg, rsa_key, validator_rsa):
    token = mint_hub_token(rsa_key, "PS256", cfg, iss="https://evil.test")
    with pytest.raises(HubAuthError):
        validator_rsa.validate(bearer(token))


def test_wrong_tier_audience_rejected(cfg, rsa_key, validator_rsa):
    token = mint_hub_token(rsa_key, "PS256", cfg, aud=["mcp://tier/external"])
    with pytest.raises(HubAuthError):
        validator_rsa.validate(bearer(token))


def test_two_tier_audiences_rejected(cfg, rsa_key, validator_rsa):
    """Exactly one tier audience — a cross-tier token never resolves."""
    token = mint_hub_token(
        rsa_key, "PS256", cfg,
        aud=["mcp://tier/internal", "mcp://tier/external"])
    with pytest.raises(HubAuthError):
        validator_rsa.validate(bearer(token))


def test_expired_token_rejected(cfg, rsa_key, validator_rsa):
    token = mint_hub_token(rsa_key, "PS256", cfg,
                           exp=int(time.time()) - 120, iat=int(time.time()) - 600)
    with pytest.raises(HubAuthError):
        validator_rsa.validate(bearer(token))


def test_missing_jti_rejected(cfg, rsa_key, validator_rsa):
    token = mint_hub_token(rsa_key, "PS256", cfg, jti=None)
    with pytest.raises(HubAuthError):
        validator_rsa.validate(bearer(token))


def test_wrong_contract_version_rejected(cfg, rsa_key, validator_rsa):
    token = mint_hub_token(rsa_key, "PS256", cfg, mcp_contract="2.0")
    with pytest.raises(HubAuthError):
        validator_rsa.validate(bearer(token))


def test_missing_contract_claim_rejected(cfg, rsa_key, validator_rsa):
    token = mint_hub_token(rsa_key, "PS256", cfg, mcp_contract=None)
    with pytest.raises(HubAuthError):
        validator_rsa.validate(bearer(token))


@pytest.mark.parametrize("header", [None, "", "Basic dXNlcjpwYXNz", "Bearer", "Bearer "])
def test_missing_or_malformed_authorization_rejected(validator_rsa, header):
    with pytest.raises(HubAuthError):
        validator_rsa.validate(header)


def test_garbage_token_rejected(validator_rsa):
    with pytest.raises(HubAuthError):
        validator_rsa.validate("Bearer not.a.jwt")


def test_custom_pins_honored(rsa_key):
    """A deployment can re-pin issuer/audience/contract without code change."""
    cfg = make_config(hub_issuer="https://other.test",
                      hub_tier_audience="mcp://tier/partner",
                      hub_contract_version="1.1")
    v = HubValidator(cfg, jwks_client=StaticJWKS(rsa_key.public_key()))
    token = mint_hub_token(rsa_key, "PS256", cfg, iss="https://other.test",
                           aud=["mcp://tier/partner"], mcp_contract="1.1")
    assert v.validate(bearer(token))["iss"] == "https://other.test"
