"""Vendor registry and OAuth-client legs (design §2/§4.5).

The registry is reviewed JSON (schema: schemas/vendor-registry.schema.json):
changing a scope ceiling is a reviewed change with token-contract-level
sign-off. Endpoints come from RFC 8414 metadata when the vendor publishes
it; hardcoded registry endpoints are allowed only for vendors without
metadata (e.g. GitHub).
"""
import json
import logging
import os

import httpx

from .client_auth import ClientAuthError, token_request_auth
from .config import Config
from .custody import Custody

log = logging.getLogger(__name__)


class VendorError(Exception):
    pass


class InvalidGrant(VendorError):
    """The vendor rejected the grant itself (revoked / rotated-away / expired)."""


class VendorUnavailable(VendorError):
    pass


class RevocationUnsupported(VendorError):
    """The vendor offers no revocation endpoint: the grant can only be
    deleted locally (it lives at the vendor until its own expiry)."""


def _unreachable(what: str, exc: Exception) -> VendorUnavailable:
    """Fixed message for a transport/parse failure; raw text (URLs, hosts) to
    the debug log only."""
    log.debug("%s failed: %r", what, exc)
    return VendorUnavailable(f"{what} unavailable")


class VendorClient:
    def __init__(self, cfg: Config, custody: Custody):
        self._registry: dict[str, dict] = json.loads(cfg.registry_path.read_text())
        self._metadata_cache: dict[str, dict] = {}
        self._custody = custody

    def registry(self) -> dict[str, dict]:
        return self._registry

    def get_vendor(self, vendor: str) -> dict | None:
        spec = self._registry.get(vendor)
        if spec is None:
            return None
        enabled_env = spec.get("enabled_env")
        if enabled_env and not os.environ.get(enabled_env):
            return None  # vendor present in registry but not configured here
        return spec

    async def endpoints(self, vendor: str) -> dict:
        """RFC 8414 metadata: authorization_endpoint / token_endpoint /
        revocation_endpoint / issuer, plus authorization_response_iss_parameter_supported
        (drives the RFC 9207 mix-up defense on the callback)."""
        spec = self._registry[vendor]
        if "auth_metadata_url" not in spec:
            return spec["endpoints"]
        if vendor not in self._metadata_cache:
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(spec["auth_metadata_url"])
                    r.raise_for_status()
                    meta = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise _unreachable(f"{vendor} authorization-server metadata", exc) from exc
            if not isinstance(meta, dict) or not all(
                    isinstance(meta.get(k), str)
                    for k in ("authorization_endpoint", "token_endpoint")):
                raise VendorUnavailable(f"{vendor} authorization-server metadata malformed")
            self._metadata_cache[vendor] = meta
        return self._metadata_cache[vendor]

    def read_client(self, vendor: str) -> dict | None:
        return self._custody.read_client(vendor)

    def _creds(self, vendor: str) -> dict:
        creds = self.read_client(vendor)
        if not creds:
            raise VendorUnavailable(f"no client credential for {vendor} in custody")
        return creds

    def _auth_for(self, vendor: str, endpoint_aud: str) -> tuple[dict, tuple | None]:
        """Form fields + basic-auth tuple per the registry's
        token_endpoint_auth_method (design §3; client_auth module)."""
        method = self._registry[vendor].get(
            "token_endpoint_auth_method", "client_secret_post")
        try:
            return token_request_auth(method, self._creds(vendor), endpoint_aud)
        except ClientAuthError as exc:
            raise VendorUnavailable(str(exc)) from exc

    async def _token_request(self, vendor: str, form: dict) -> dict:
        """POST to the vendor token endpoint with client auth; classify failures."""
        eps = await self.endpoints(vendor)
        auth_form, basic = self._auth_for(vendor, eps["token_endpoint"])
        form = {**form, **auth_form}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(eps["token_endpoint"], data=form, auth=basic,
                                 headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise _unreachable("vendor token endpoint", exc) from exc
        if r.status_code >= 500:
            raise VendorUnavailable(f"vendor token endpoint {r.status_code}")
        body = {}
        if "json" in r.headers.get("content-type", ""):
            try:
                body = r.json()
            except ValueError as exc:
                raise _unreachable("vendor token endpoint response", exc) from exc
            if not isinstance(body, dict):
                raise VendorUnavailable("vendor token endpoint response malformed")
        # GitHub answers 200 with an error field; RFC-shaped vendors answer 400.
        error = body.get("error")
        if r.status_code >= 400 or error:
            if error in ("invalid_grant", "bad_refresh_token"):
                raise InvalidGrant(error)
            raise VendorUnavailable(f"token endpoint error: {error or r.status_code}")
        if not isinstance(body.get("access_token"), str) or not body["access_token"]:
            raise VendorUnavailable("vendor token endpoint response has no access_token")
        return body

    async def exchange_code(self, vendor: str, code: str, verifier: str,
                            redirect_uri: str) -> dict:
        return await self._token_request(vendor, {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        })

    async def refresh(self, vendor: str, refresh_token: str) -> dict:
        return await self._token_request(vendor, {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })

    async def vendor_user_id(self, vendor: str, access_token: str) -> str:
        """Best-effort: the id is for audit joins only, so any failure yields
        "unknown" rather than failing a consent whose code is already spent."""
        try:
            spec = self._registry[vendor]
            url = spec.get("vendor_user_endpoint")
            if not url:
                url = (await self.endpoints(vendor)).get("userinfo_endpoint")
            if not url:
                return "unknown"
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(url, headers={"Authorization": f"Bearer {access_token}",
                                              "Accept": "application/json"})
            if r.status_code != 200:
                return "unknown"
            body = r.json()
        except (httpx.HTTPError, ValueError, VendorError) as exc:
            log.debug("vendor user lookup failed for %s: %r", vendor, exc)
            return "unknown"
        if not isinstance(body, dict):
            return "unknown"
        return str(body.get("id") or body.get("login") or body.get("sub") or "unknown")

    async def revoke(self, vendor: str, entry: dict) -> None:
        """RFC 7009 where offered; GitHub's grant-deletion API as the documented
        per-vendor deviation (design §2 'deviations documented per vendor')."""
        spec = self._registry[vendor]
        kind = spec.get("revocation", {}).get("type", "rfc7009")
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                if kind == "github_grant":
                    creds = self._creds(vendor)
                    r = await c.request(
                        "DELETE",
                        f"https://api.github.com/applications/{creds['client_id']}/grant",
                        auth=(creds["client_id"], creds["client_secret"]),
                        json={"access_token": entry["access_token"]},
                        headers={"Accept": "application/vnd.github+json"})
                    if r.status_code not in (204, 404, 422):
                        raise VendorUnavailable(f"github grant delete {r.status_code}")
                else:
                    eps = await self.endpoints(vendor)
                    if not eps.get("revocation_endpoint"):
                        raise RevocationUnsupported(f"{vendor} has no revocation endpoint")
                    # The assertion audience stays the token endpoint (RFC 7523
                    # accepts any identifier of the AS).
                    auth_form, basic = self._auth_for(vendor, eps["token_endpoint"])
                    r = await c.post(eps["revocation_endpoint"], auth=basic,
                                     data={"token": entry["refresh_token"],
                                           "token_type_hint": "refresh_token",
                                           **auth_form})
                    if r.status_code >= 400:
                        raise VendorUnavailable(f"revocation endpoint {r.status_code}")
        except httpx.HTTPError as exc:
            raise _unreachable("vendor revocation", exc) from exc
