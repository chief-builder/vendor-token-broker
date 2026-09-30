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
import re
from collections.abc import Mapping

import httpx

from .client_auth import ClientAuthError, token_request_auth
from .config import Config, ConfigError
from .custody import Custody

log = logging.getLogger(__name__)


# GitHub's grant-deletion API (the documented RFC 7009 deviation); a registry
# entry may override it with revocation.grant_url (GitHub Enterprise Server).
GITHUB_GRANT_URL = "https://api.github.com/applications/{client_id}/grant"

VENDOR_ID = re.compile(r"^[a-z0-9-]+$")  # also the registry schema's key pattern


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
    def __init__(self, cfg: Config, custody: Custody, environ: Mapping[str, str] | None = None):
        self._registry: dict[str, dict] = json.loads(cfg.registry_path.read_text())
        # Vendor ids become custody path segments: enforce the schema's
        # pattern at load, not only in CI, so no id can nest or traverse.
        bad = [v for v in self._registry if not VENDOR_ID.match(v)]
        if bad:
            raise ConfigError(f"registry vendor ids must match {VENDOR_ID.pattern}: {bad}")
        # A vendor with `enabled_env` is served only where that variable is
        # set (its client is configured here). Decided once, at startup.
        environ = os.environ if environ is None else environ
        self._enabled = frozenset(
            v
            for v, spec in self._registry.items()
            if not spec.get("enabled_env") or environ.get(spec["enabled_env"])
        )
        self._timeout = cfg.vendor_timeout_s
        self._metadata_cache: dict[str, dict] = {}
        self._custody = custody

    def registry(self) -> dict[str, dict]:
        return self._registry

    def get_vendor(self, vendor: str) -> dict | None:
        """The registry entry, or None if unknown or not enabled here."""
        return self._registry[vendor] if vendor in self._enabled else None

    def resource(self, vendor: str) -> str | None:
        """RFC 8707 resource indicator for vendors whose tokens are bound to
        an MCP server (the MCP authorization spec requires it on authorize,
        code exchange, and refresh). None for ordinary vendor APIs."""
        return self._registry[vendor].get("resource")

    async def endpoints(self, vendor: str) -> dict:
        """RFC 8414 metadata: authorization_endpoint / token_endpoint /
        revocation_endpoint / issuer, plus authorization_response_iss_parameter_supported
        (drives the RFC 9207 mix-up defense on the callback)."""
        spec = self._registry[vendor]
        if "auth_metadata_url" not in spec:
            return spec["endpoints"]
        if vendor not in self._metadata_cache:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as c:
                    r = await c.get(spec["auth_metadata_url"])
                    r.raise_for_status()
                    meta = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise _unreachable(f"{vendor} authorization-server metadata", exc) from exc
            if not isinstance(meta, dict) or not all(
                isinstance(meta.get(k), str) for k in ("authorization_endpoint", "token_endpoint")
            ):
                raise VendorUnavailable(f"{vendor} authorization-server metadata malformed")
            self._metadata_cache[vendor] = meta
        return self._metadata_cache[vendor]

    async def read_client(self, vendor: str) -> dict | None:
        return await self._custody.read_client(vendor)

    async def _creds(self, vendor: str) -> dict:
        creds = await self.read_client(vendor)
        if not creds:
            raise VendorUnavailable(f"no client credential for {vendor} in custody")
        return creds

    async def _auth_for(self, vendor: str, endpoint_aud: str) -> tuple[dict, httpx.Auth]:
        """Form fields + HTTP auth per the registry's token_endpoint_auth_method
        (design §3; client_auth module). The auth is HTTP Basic for
        client_secret_basic and a no-op otherwise."""
        method = self._registry[vendor].get("token_endpoint_auth_method", "client_secret_post")
        try:
            form, basic = token_request_auth(method, await self._creds(vendor), endpoint_aud)
        except ClientAuthError as exc:
            raise VendorUnavailable(str(exc)) from exc
        return form, httpx.BasicAuth(*basic) if basic else httpx.Auth()

    async def _token_request(self, vendor: str, form: dict) -> dict:
        """POST to the vendor token endpoint with client auth; classify failures."""
        eps = await self.endpoints(vendor)
        auth_form, basic = await self._auth_for(vendor, eps["token_endpoint"])
        form = {**form, **auth_form}
        # Some MCP authorization servers (Linear) refuse `resource` on refresh
        # and keep the refreshed token bound to the original resource anyway.
        refresh = form.get("grant_type") == "refresh_token"
        if (resource := self.resource(vendor)) and (
            not refresh or self._registry[vendor].get("resource_on_refresh", True)
        ):
            form["resource"] = resource
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                r = await c.post(
                    eps["token_endpoint"],
                    data=form,
                    auth=basic,
                    headers={"Accept": "application/json"},
                )
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

    async def exchange_code(self, vendor: str, code: str, verifier: str, redirect_uri: str) -> dict:
        return await self._token_request(
            vendor,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
        )

    async def refresh(self, vendor: str, refresh_token: str) -> dict:
        return await self._token_request(
            vendor,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )

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
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                r = await c.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                )
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
        per-vendor deviation (design §2.1)."""
        spec = self._registry[vendor]
        kind = spec.get("revocation", {}).get("type", "rfc7009")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                if kind == "github_grant":
                    creds = await self._creds(vendor)
                    if not entry.get("access_token"):
                        return  # nothing live to revoke (e.g. a scrubbed STALE entry)
                    r = await c.request(
                        "DELETE",
                        spec["revocation"]
                        .get("grant_url", GITHUB_GRANT_URL)
                        .format(client_id=creds["client_id"]),
                        auth=(creds["client_id"], creds["client_secret"]),
                        json={"access_token": entry["access_token"]},
                        headers={"Accept": "application/vnd.github+json"},
                    )
                    if r.status_code not in (204, 404, 422):
                        raise VendorUnavailable(f"github grant delete {r.status_code}")
                else:
                    # Revoke the access token, then the refresh token. RFC 7009
                    # §2.1 says revoking a refresh token should take its access
                    # tokens with it, but not every vendor does (Linear's live
                    # on for up to 24 h). A server may refuse access-token
                    # revocation (unsupported_token_type): only the refresh
                    # token's answer decides. A scrubbed STALE entry has neither.
                    tokens = [
                        (entry[h], h) for h in ("access_token", "refresh_token") if entry.get(h)
                    ]
                    if not tokens:
                        return
                    eps = await self.endpoints(vendor)
                    if not eps.get("revocation_endpoint"):
                        raise RevocationUnsupported(f"{vendor} has no revocation endpoint")
                    # The assertion audience stays the token endpoint (RFC 7523
                    # accepts any identifier of the AS).
                    for token, hint in tokens:
                        auth_form, basic = await self._auth_for(vendor, eps["token_endpoint"])
                        r = await c.post(
                            eps["revocation_endpoint"],
                            auth=basic,
                            data={"token": token, "token_type_hint": hint, **auth_form},
                        )
                        last = hint == tokens[-1][1]
                        if r.status_code >= 500 or (last and r.status_code >= 400):
                            raise VendorUnavailable(f"revocation endpoint {r.status_code}")
        except httpx.HTTPError as exc:
            raise _unreachable("vendor revocation", exc) from exc
