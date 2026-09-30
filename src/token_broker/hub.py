"""Hub JWT re-validation (design §3): never trust the gateway.

The broker independently verifies issuer, signature, expiry, and tier
audience on every inbound call. Algorithms are pinned to the configured
set (PS256 primary, ES256 permitted by default; RS256 and all HMAC
forbidden). Contract shape — the pinned contract version and exactly one
tier audience — is enforced here so a malformed or cross-tier token never
reaches the resolve path.
"""

import asyncio
import logging

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError

from .config import Config

TIER_PREFIX = "mcp://tier/"
# How long a fetched hub JWKS is trusted before it is fetched again. A key
# the hub removes keeps validating for at most this long.
JWKS_CACHE_S = 300
log = logging.getLogger(__name__)


class HubAuthError(Exception):
    """The presented token is not a valid hub JWT (→ 401)."""


class HubUnavailable(Exception):
    """The hub's JWKS could not be fetched: an outage, not a bad token (→ 503)."""


class HubValidator:
    def __init__(self, cfg: Config, jwks_client=None):
        self._cfg = cfg
        # cache_keys stays off: PyJWT's per-kid cache never expires, so a key
        # the hub removed from its JWKS would stay trusted until restart. The
        # JWK-set cache (JWKS_CACHE_S) avoids per-request fetches and bounds
        # how long a removed key is still accepted.
        self._jwks = jwks_client or PyJWKClient(
            cfg.hub_jwks_uri, timeout=cfg.jwks_timeout_s, lifespan=JWKS_CACHE_S
        )

    async def verify(self, authorization: str | None) -> dict:
        """Async entry point for the routes: validate() in a worker thread,
        because a JWKS fetch is a blocking HTTP call (up to the client
        timeout) that must never stall the event loop."""
        return await asyncio.to_thread(self.validate, authorization)

    async def check(self) -> None:
        """Fetch the hub JWKS once (startup check). Raises HubUnavailable if
        it cannot be fetched, HubAuthError if it holds no usable keys."""
        await asyncio.to_thread(self._check)

    def _check(self) -> None:
        try:
            self._jwks.get_signing_keys()
        except (PyJWKClientConnectionError, TimeoutError) as exc:
            log.debug("hub JWKS fetch failed: %s", exc)
            raise HubUnavailable("hub signing keys unavailable") from exc
        except Exception as exc:  # e.g. "did not contain any signing keys"
            raise HubAuthError(str(exc)) from exc

    def signing_key(self, token: str):
        """The hub key that signed `token` (blocking; worker thread only).
        Raises HubUnavailable when the JWKS cannot be fetched and
        HubAuthError for an unknown kid or a malformed token."""
        try:
            return self._jwks.get_signing_key_from_jwt(token).key
        except (PyJWKClientConnectionError, TimeoutError) as exc:
            log.debug("hub JWKS fetch failed: %s", exc)
            raise HubUnavailable("hub signing keys unavailable") from exc
        except Exception as exc:  # unknown kid, malformed token, ...
            raise HubAuthError(str(exc)) from exc

    def validate(self, authorization: str | None) -> dict:
        """Return verified claims of the Bearer hub JWT. Raises HubAuthError for a
        bad token, HubUnavailable when the hub JWKS cannot be fetched."""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HubAuthError("missing bearer token")
        parts = authorization.split(None, 1)
        if len(parts) != 2 or not parts[1]:
            raise HubAuthError("missing bearer token")
        token = parts[1]
        key = self.signing_key(token)
        try:
            claims = jwt.decode(
                token,
                key,
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
