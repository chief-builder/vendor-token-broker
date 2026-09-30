"""The gateway's two HTTP dependencies: the hub (RFC 8693 token exchange)
and the broker (resolve, grant listing, self-service disconnect). Errors
carry ids and statuses only — never token material."""

from dataclasses import dataclass
from urllib.parse import quote

import httpx
import jwt

from .config import GatewayConfig

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class HandoffError(Exception):
    """The hub refused the exchange: a gateway or token problem, not an outage."""


class Unavailable(Exception):
    """A dependency is down or answered 5xx: retry later, never 'connect'."""


class Hub:
    def __init__(self, cfg: GatewayConfig, http: httpx.AsyncClient):
        self._cfg, self._http = cfg, http

    async def exchange(self, mcp_token: str) -> str:
        """MCP access token (aud = this gateway) -> hub JWT for the broker."""
        try:
            r = await self._http.post(
                self._cfg.hub_token_endpoint,
                data={
                    "grant_type": TOKEN_EXCHANGE,
                    "client_id": self._cfg.client_id,
                    "client_secret": self._cfg.client_secret,
                    "subject_token": mcp_token,
                    "subject_token_type": ACCESS_TOKEN_TYPE,
                    "requested_token_type": ACCESS_TOKEN_TYPE,
                    "scope": self._cfg.exchange_scope,
                },
            )
        except httpx.HTTPError as exc:
            raise Unavailable(f"hub unreachable ({type(exc).__name__})") from exc
        if r.status_code >= 500:
            raise Unavailable(f"hub answered {r.status_code}")
        if r.status_code != 200:
            error = (
                r.json().get("error", "")
                if r.headers.get("content-type", "").startswith("application/json")
                else ""
            )
            raise HandoffError(f"hub refused token exchange ({r.status_code} {error})".strip())
        return r.json()["access_token"]


@dataclass(frozen=True)
class Resolved:
    token: str | None = None  # 200: the vendor access token
    consent_url: str | None = None  # 404/409: where the user connects the vendor
    problem: str = ""  # problem title, for errors and logs


class Broker:
    def __init__(self, cfg: GatewayConfig, http: httpx.AsyncClient):
        self._cfg, self._http = cfg, http

    async def resolve(self, hub_jwt: str, vendor: str) -> Resolved:
        try:
            r = await self._http.post(
                f"{self._cfg.broker_url}/v1/tokens/resolve",
                json={"vendor": vendor, "min_ttl_s": self._cfg.min_ttl_s},
                headers={"Authorization": f"Bearer {hub_jwt}"},
            )
        except httpx.HTTPError as exc:
            raise Unavailable(f"broker unreachable ({type(exc).__name__})") from exc
        body = r.json() if r.headers.get("content-type", "").endswith("json") else {}
        title = body.get("title", "")
        if r.status_code == 200:
            return Resolved(token=body["access_token"])
        if r.status_code in (404, 409) and body.get("authorize_uri"):
            return Resolved(consent_url=body["authorize_uri"], problem=title)
        if r.status_code == 401:
            raise HandoffError(f"broker rejected the hub token ({title or 401})")
        if r.status_code >= 500:
            raise Unavailable(f"broker answered {r.status_code} {title}".strip())
        return Resolved(problem=title or str(r.status_code))  # e.g. 409 revoke-pending

    async def connected(self, hub_jwt: str, vendor: str) -> bool:
        """True once the user holds an ACTIVE grant for the vendor. Used while
        waiting on the browser flow: unlike resolve, it mints no new link."""
        try:
            r = await self._http.get(
                f"{self._cfg.broker_url}/v1/grants", headers={"Authorization": f"Bearer {hub_jwt}"}
            )
        except httpx.HTTPError as exc:
            raise Unavailable(f"broker unreachable ({type(exc).__name__})") from exc
        if r.status_code == 401:
            raise HandoffError("broker rejected the hub token (401)")
        if r.status_code != 200:  # an outage must never look like "not connected yet"
            raise Unavailable(f"broker answered {r.status_code} listing grants")
        return any(
            g.get("vendor") == vendor and g.get("state") == "ACTIVE"
            for g in r.json().get("grants", [])
        )

    async def disconnect(self, hub_jwt: str, vendor: str) -> str:
        """Revoke and delete the user's grant (the broker's self-service
        DELETE, vendor first). Returns "revoked", "unsupported" (the vendor
        can't cancel tokens: deleted here only), "not-connected", or
        "pending" (the vendor is down: the broker keeps retrying, and the
        grant stays unusable meanwhile)."""
        # The broker checks the path's sub against the hub JWT it verifies.
        sub = jwt.decode(hub_jwt, options={"verify_signature": False})["sub"]
        url = f"{self._cfg.broker_url}/v1/grants/{quote(vendor, safe='')}/{quote(sub, safe='/')}"
        try:
            r = await self._http.delete(url, headers={"Authorization": f"Bearer {hub_jwt}"})
        except httpx.HTTPError as exc:
            raise Unavailable(f"broker unreachable ({type(exc).__name__})") from exc
        body = r.json() if r.headers.get("content-type", "").endswith("json") else {}
        title = body.get("title", "")
        if r.status_code == 200:
            return "unsupported" if body.get("vendor_revocation") == "unsupported" else "revoked"
        if r.status_code == 404 and title == "no-grant":
            return "not-connected"
        if r.status_code == 502 and title == "revoke-pending":
            return "pending"
        if r.status_code >= 500:
            raise Unavailable(f"broker answered {r.status_code} {title}".strip())
        raise HandoffError(f"broker refused the disconnect ({r.status_code} {title})".strip())
