"""Hub JWT re-validation (design §3): never trust the gateway.

The broker independently verifies issuer, signature, expiry, and tier
audience on every inbound call. Algorithms are pinned to the configured
set (PS256 primary, ES256 permitted by default; RS256 and all HMAC
forbidden). Contract shape — the pinned contract version and exactly one
tier audience — is enforced here so a malformed or cross-tier token never
reaches the resolve path.
"""
import jwt
from jwt import PyJWKClient

from .config import Config

TIER_PREFIX = "mcp://tier/"


class HubAuthError(Exception):
    pass


class HubValidator:
    def __init__(self, cfg: Config, jwks_client=None):
        self._cfg = cfg
        # cache_keys stays off: PyJWT's per-kid cache never expires, so a key
        # the hub removed from its JWKS would stay trusted until restart. The
        # JWK-set cache (300s lifespan) already avoids per-request fetches.
        self._jwks = jwks_client or PyJWKClient(cfg.hub_jwks_uri)

    def validate(self, authorization: str | None) -> dict:
        """Return verified claims of the Bearer hub JWT or raise HubAuthError."""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HubAuthError("missing bearer token")
        parts = authorization.split(None, 1)
        if len(parts) != 2 or not parts[1]:
            raise HubAuthError("missing bearer token")
        token = parts[1]
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key,
                algorithms=list(self._cfg.hub_algorithms),
                issuer=self._cfg.hub_issuer,
                audience=self._cfg.hub_tier_audience,
                leeway=30,
                options={"require": ["exp", "iat", "sub", "jti"]},
            )
        except Exception as exc:  # jwt raises many subclasses; all mean 401
            raise HubAuthError(str(exc)) from exc
        if claims.get("mcp_contract") != self._cfg.hub_contract_version:
            raise HubAuthError("unsupported mcp_contract")
        aud = claims.get("aud", [])
        aud = [aud] if isinstance(aud, str) else aud
        if sum(a.startswith(TIER_PREFIX) for a in aud) != 1:
            raise HubAuthError("token must carry exactly one tier audience")
        return claims
