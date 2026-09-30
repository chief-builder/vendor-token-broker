"""Hub login for the consent leg (review H1, design §6).

Before a user links a vendor account, the browser that opened the
authorize link must prove, by logging in at the hub, that it belongs to the
user the link was issued for. The broker acts as an ordinary OIDC relying
party here: authorization code + PKCE + nonce, then it validates the hub's
ID token (signature from the hub JWKS, issuer, audience, expiry, nonce). It
consumes a hub-issued token and issues nothing, so the custodian-not-issuer
rule holds.
"""

import asyncio
import hmac
import logging
from urllib.parse import urlencode

import httpx
import jwt
from jwt.exceptions import PyJWKClientConnectionError

from .config import Config
from .hub import HubUnavailable, HubValidator

log = logging.getLogger(__name__)


class HubLoginError(Exception):
    """The hub login did not produce a valid identity for this flow."""


class HubLogin:
    def __init__(self, cfg: Config, validator: HubValidator):
        self._cfg = cfg
        self._validator = validator
        self._meta: dict | None = None

    async def check(self) -> None:
        """Startup check: the hub's OIDC discovery document is reachable and
        names this issuer."""
        await self._metadata()

    async def _metadata(self) -> dict:
        if self._meta is not None:
            return self._meta
        url = (
            self._cfg.hub_discovery_url
            or f"{self._cfg.hub_issuer.rstrip('/')}/.well-known/openid-configuration"
        )
        try:
            async with httpx.AsyncClient(timeout=self._cfg.jwks_timeout_s) as c:
                r = await c.get(url)
                r.raise_for_status()
                meta = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("hub discovery failed: %r", exc)
            raise HubUnavailable("hub login metadata unavailable") from exc
        if (
            not isinstance(meta, dict)
            or meta.get("issuer") != self._cfg.hub_issuer
            or not all(
                isinstance(meta.get(k), str) for k in ("authorization_endpoint", "token_endpoint")
            )
        ):
            raise HubLoginError("hub discovery document is malformed or names another issuer")
        self._meta = meta
        return meta

    async def authorization_url(
        self, *, state: str, nonce: str, challenge: str, login_hint: str | None, redirect_uri: str
    ) -> str:
        meta = await self._metadata()
        params = {
            "client_id": self._cfg.hub_login_client_id,
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    async def exchange(self, *, code: str, verifier: str, redirect_uri: str, nonce: str) -> dict:
        """Redeem the hub code and return the validated ID-token claims."""
        meta = await self._metadata()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "client_id": self._cfg.hub_login_client_id,
        }
        if self._cfg.hub_login_client_secret:
            form["client_secret"] = self._cfg.hub_login_client_secret
        try:
            async with httpx.AsyncClient(timeout=self._cfg.jwks_timeout_s) as c:
                r = await c.post(
                    meta["token_endpoint"], data=form, headers={"Accept": "application/json"}
                )
        except httpx.HTTPError as exc:
            log.debug("hub token endpoint failed: %r", exc)
            raise HubUnavailable("hub token endpoint unavailable") from exc
        if r.status_code >= 500:
            raise HubUnavailable(f"hub token endpoint {r.status_code}")
        try:
            body = r.json()
        except ValueError as exc:
            raise HubLoginError("hub token response is not JSON") from exc
        if (
            r.status_code >= 400
            or not isinstance(body, dict)
            or not isinstance(body.get("id_token"), str)
        ):
            raise HubLoginError(f"hub code exchange failed ({r.status_code})")
        return await asyncio.to_thread(self._validate_id_token, body["id_token"], nonce)

    def _validate_id_token(self, id_token: str, nonce: str) -> dict:
        try:
            key = self._validator._jwks.get_signing_key_from_jwt(id_token).key
        except (PyJWKClientConnectionError, TimeoutError) as exc:
            raise HubUnavailable("hub signing keys unavailable") from exc
        except Exception as exc:
            raise HubLoginError(f"ID token key: {exc}") from exc
        try:
            claims = jwt.decode(
                id_token,
                key,
                algorithms=list(self._cfg.hub_algorithms),
                issuer=self._cfg.hub_issuer,
                audience=self._cfg.hub_login_client_id,
                leeway=30,
                options={"require": ["exp", "iat", "sub", "nonce"]},
            )
        except Exception as exc:
            raise HubLoginError(f"ID token invalid: {exc}") from exc
        if not hmac.compare_digest(str(claims["nonce"]), nonce):
            raise HubLoginError("ID token nonce mismatch")
        return claims
