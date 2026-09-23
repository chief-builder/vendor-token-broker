"""Vendor Token Broker — app factory and the 7-route surface.

Custodian, not issuer (design §1/§10): this service holds no signing keys
and exposes no token-minting or JWKS endpoint — tests/unit/test_routes.py
audits the route table for exactly that on every push.

Run: uvicorn --factory token_broker.main:create_app
"""
import asyncio
import base64
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import __version__, sweeper
from . import refresh as refresh_mod
from . import vendors as vendors_mod
from .audit import audit
from .config import Config
from .coordination import CoordinationUnavailable, make_coordination
from .custody import CasConflict, CustodyUnavailable, VaultStore
from .hub import HubAuthError, HubUnavailable, HubValidator
from .problems import Problems
from .refresh import entry_from_token_response
from .vendors import VendorClient


def consent_scopes(required: list[str], ceiling: list[str]) -> list[str]:
    """§4.2/§6: request the minimum — the tool's required scopes capped by the
    registry ceiling. No required_scopes (legacy caller) → the full ceiling."""
    if not required:
        return list(ceiling)
    return [s for s in required if s in ceiling]


def reconsent_scopes(held: list[str], required: list[str], ceiling: list[str]) -> list[str]:
    """§4.1: re-consent for the union of held and required scopes, capped by
    the ceiling (ceiling order preserved)."""
    return [s for s in ceiling if s in set(held) | set(required)]


# Browser-facing responses carry the vendor code/state in the URL: never
# cache them, never leak the URL as a Referer, never sniff the content type.
BROWSER_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                   "X-Content-Type-Options": "nosniff"}


def page(text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"<h1>{text}</h1>", status_code=status, headers=BROWSER_HEADERS)


def parse_resolve_body(raw: bytes) -> tuple[dict | None, str | None]:
    """Validate the resolve request body. Returns (body, None) or (None, why)."""
    try:
        body = json.loads(raw)
    except ValueError:
        return None, "body must be JSON"
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    if not isinstance(body.get("vendor"), str) or not body["vendor"]:
        return None, "vendor must be a non-empty string"
    if body.get("sub") is not None and not isinstance(body["sub"], str):
        return None, "sub must be a string"
    ttl = body.get("min_ttl_s", 120)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
        return None, "min_ttl_s must be a non-negative integer"
    scopes = body.get("required_scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(x, str) for x in scopes):
        return None, "required_scopes must be a list of strings"
    return body, None


def ok_response(entry: dict) -> JSONResponse:
    return JSONResponse({"access_token": entry["access_token"],
                         "expires_at": entry["expires_at"],
                         "granted_scopes": entry["granted_scopes"]})


class Broker:
    """All per-process broker state; routes and the sweeper operate on this."""

    def __init__(self, cfg: Config, *, custody=None, hub=None, vendors=None,
                 coord=None):
        self.cfg = cfg
        self.instance_id = secrets.token_hex(8)
        self.problem = Problems(cfg.problem_urn_prefix)
        self.custody = custody or VaultStore(cfg)
        self.hub = hub or HubValidator(cfg)
        self.vendors = vendors or VendorClient(cfg, self.custody)
        self.coord = coord or make_coordination(cfg, self.instance_id)
        self.cache: dict[tuple[str, str], tuple[dict, int, float]] = {}
        self.sweep_cursor = 0   # where the next budgeted sweep pass resumes

    async def new_txn(self, sub: str, vendor: str, scopes: list[str]) -> str:
        txn_id = secrets.token_urlsafe(24)
        await self.coord.put_txn(txn_id, {"sub": sub, "vendor": vendor,
                                          "scopes": scopes,
                                          "created_at": time.time()})
        return txn_id

    async def needs_consent(self, sub: str, vendor: str, spec: dict,
                            scopes: list[str] | None = None) -> JSONResponse:
        txn = await self.new_txn(
            sub, vendor, scopes if scopes is not None else spec.get("scope_ceiling", []))
        return self.problem(
            404, "needs-consent", f"no usable grant for {vendor}",
            authorize_uri=f"{self.cfg.broker_public_url}/v1/authorize/{vendor}?txn={txn}")

    async def get_entry(self, vendor: str, sub: str):
        key = (vendor, sub)
        cached = self.cache.get(key)
        if cached and time.time() - cached[2] < self.cfg.cache_ttl_s:
            return cached[0], cached[1]
        found = await self.custody.read(vendor, sub)
        if found is None:
            self.cache.pop(key, None)
            return None
        self.cache[key] = (found[0], found[1], time.time())
        return found

    def put_cache(self, vendor: str, sub: str, entry: dict, ver: int) -> None:
        self.cache[(vendor, sub)] = (entry, ver, time.time())

    def drop_cache(self, vendor: str, sub: str) -> None:
        self.cache.pop((vendor, sub), None)

    async def invalidate(self, vendor: str, sub: str) -> None:
        """Local drop + best-effort cross-replica broadcast (revoke/STALE/
        delete). Worst case without the broadcast is the documented ≤60s
        per-replica cache TTL."""
        self.drop_cache(vendor, sub)
        try:
            await self.coord.publish_invalidate(vendor, sub)
        except CoordinationUnavailable:
            pass

    async def mark_stale(self, vendor: str, sub: str, generation: int) -> None:
        """Emit the per-entry broker.stale record and, when STALEs for one
        vendor burst past the threshold within the window, a mass-stale page
        (§8/§9 — the org-App-uninstall anomaly)."""
        audit("broker.stale", sub=sub, vendor=vendor, generation=generation)
        try:
            count, page = await self.coord.record_stale(vendor)
        except CoordinationUnavailable:
            return  # the per-entry record above still stands
        if page:
            audit("broker.stale.mass", vendor=vendor, count=count,
                  window_s=self.cfg.mass_stale_window_s, page=True,
                  security_event=True)


def create_app(cfg: Config | None = None, broker: Broker | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    b = broker or Broker(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await b.coord.start(on_invalidate=b.drop_cache)
        task = (asyncio.create_task(sweeper.sweep_loop(b))
                if cfg.sweep_interval_s > 0 else None)
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
            await b.coord.close()

    # No /docs, /redoc, /openapi.json: the served surface is exactly the 7
    # routes (tests/unit/test_routes.py audits app.routes, not the schema).
    app = FastAPI(title="vendor-token-broker", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.broker = b
    problem = b.problem

    async def hub_claims(request: Request) -> tuple[dict | None, JSONResponse | None]:
        """Validate the caller's hub JWT: (claims, None) or (None, problem).
        A bad token is 401; an unreachable hub JWKS is a retriable 503."""
        try:
            return await b.hub.verify(request.headers.get("authorization")), None
        except HubUnavailable as exc:
            return None, problem(503, "hub-unavailable", str(exc))
        except HubAuthError as exc:
            return None, problem(401, "invalid-hub-token", str(exc))

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/v1/tokens/resolve")
    async def resolve(request: Request):
        claims, denied = await hub_claims(request)
        if denied is not None:
            return denied
        body, invalid = parse_resolve_body(await request.body())
        if invalid is not None:
            audit("broker.resolve", decision="deny", reason="invalid_request",
                  hub_jti=claims.get("jti"), sub=claims["sub"])
            return problem(400, "invalid-request", invalid)
        vendor = body["vendor"]
        sub = claims["sub"]  # the JWT is authoritative; the field is advisory
        if body.get("sub") and body["sub"] != sub:
            audit("broker.resolve", decision="deny", reason="sub_mismatch",
                  hub_jti=claims.get("jti"), sub=sub, claimed_sub=body["sub"], vendor=vendor)
            return problem(400, "sub-mismatch", "request sub does not match hub token")
        min_ttl = body.get("min_ttl_s", 120)
        spec = b.vendors.get_vendor(vendor)
        if spec is None:
            return problem(404, "unknown-vendor", f"vendor {vendor} not registered/enabled")

        ceiling = spec.get("scope_ceiling", [])
        required = list(body.get("required_scopes", []))
        if not ceiling:
            # Empty ceiling: scopes are governed vendor-side (e.g. GitHub App
            # permissions). The broker neither requests nor enforces them.
            required = []
        if not set(required) <= set(ceiling):
            # A caller can never widen past the registry ceiling (§4.2).
            audit("broker.resolve", decision="deny", path="scope-ceiling",
                  hub_jti=claims.get("jti"), sub=sub, vendor=vendor,
                  required=required, ceiling=ceiling)
            return problem(403, "scope-exceeds-ceiling",
                           "required_scopes exceed the vendor registry ceiling",
                           required=required, ceiling=ceiling)
        want = consent_scopes(required, ceiling)

        def _audit(decision: str, path: str, **kw):
            audit("broker.resolve", decision=decision, path=path,
                  hub_jti=claims.get("jti"), sub=sub, vendor=vendor, **kw)

        async def _insufficient_scope(entry: dict) -> JSONResponse | None:
            """§4.1: an ACTIVE grant that doesn't cover the tool's required
            scopes returns 409 needs-reconsent-scope with an authorize_uri that
            re-consents for the union of held and required scopes (≤ ceiling)."""
            missing = [s for s in required if s not in entry["granted_scopes"]]
            if not missing:
                return None
            txn = await b.new_txn(
                sub, vendor, reconsent_scopes(entry["granted_scopes"], required, ceiling))
            _audit("deny", "insufficient-scope", missing=missing)
            return problem(
                409, "needs-reconsent-scope",
                "grant does not cover the required scopes",
                missing_scopes=missing,
                authorize_uri=f"{cfg.broker_public_url}/v1/authorize/{vendor}?txn={txn}")

        try:
            found = await b.get_entry(vendor, sub)
            if found is None:
                _audit("needs-consent", "absent")
                return await b.needs_consent(sub, vendor, spec, want)
            entry, ver = found
            if entry["state"] == "STALE":
                _audit("needs-consent", "stale")
                return await b.needs_consent(sub, vendor, spec, want)
            if entry["state"] == "REVOKE_PENDING":
                _audit("deny", "revoke-pending")
                return problem(409, "revoke-pending", "entry is being revoked")

            insufficient = await _insufficient_scope(entry)
            if insufficient is not None:
                return insufficient

            remaining = entry["expires_at"] - time.time()
            if remaining >= max(min_ttl, cfg.refresh_buffer_s):
                _audit("allow", "cache")
                return ok_response(entry)

            # Inside the refresh buffer: single-flight per {vendor, sub} (§8).
            gen_before = entry["refresh_generation"]

            async def _serve_if_gen_advanced():
                """Waiter retry check (redis profile): re-read each retry and
                serve without the lock if another replica's refresh already
                advanced the generation (ADR-0001)."""
                b.drop_cache(vendor, sub)
                f = await b.get_entry(vendor, sub)
                if f and f[0]["state"] == "ACTIVE" and \
                        f[0]["refresh_generation"] != gen_before and \
                        f[0]["expires_at"] - time.time() >= min_ttl:
                    _audit("allow", "refresh-waited",
                           generation=f[0]["refresh_generation"])
                    return ok_response(f[0])
                return None

            lock_token, early = await b.coord.wait_refresh_lock(
                vendor, sub, _serve_if_gen_advanced)
            if early is not None:
                return early
            if lock_token is None:
                # Lock-holder death path: re-read and serve if usable (§8 rules).
                b.drop_cache(vendor, sub)
                found = await b.get_entry(vendor, sub)
                if found and found[0]["state"] == "ACTIVE" and \
                        found[0]["expires_at"] - time.time() >= min_ttl:
                    _audit("allow", "lock-timeout-reread")
                    return ok_response(found[0])
                return problem(503, "vendor-unavailable", "refresh lock timeout")
            try:
                b.drop_cache(vendor, sub)
                found = await b.get_entry(vendor, sub)
                if found is None:
                    _audit("needs-consent", "absent")
                    return await b.needs_consent(sub, vendor, spec, want)
                entry, ver = found
                if entry["state"] == "STALE":
                    _audit("needs-consent", "stale")
                    return await b.needs_consent(sub, vendor, spec, want)
                if entry["state"] == "REFRESHING" and \
                        not refresh_mod.abandoned(entry, cfg.refreshing_ttl_s):
                    # In flight on another replica whose lock TTL'd out from
                    # under it. Never re-refresh (a rotating RT would burn the
                    # family) — serve the still-valid token or ask for a retry.
                    if entry["expires_at"] - time.time() >= min_ttl:
                        _audit("allow", "refresh-in-progress")
                        return ok_response(entry)
                    _audit("deny", "refresh-in-progress")
                    return problem(503, "vendor-unavailable",
                                   "refresh in progress; retry")
                if entry["refresh_generation"] != gen_before and \
                        entry["expires_at"] - time.time() >= min_ttl:
                    # A concurrent refresh already won — same token, no vendor call.
                    _audit("allow", "refresh-waited",
                           generation=entry["refresh_generation"])
                    return ok_response(entry)

                outcome = await refresh_mod.attempt_refresh(b, vendor, sub, entry, ver)
                if isinstance(outcome, refresh_mod.Refreshed):
                    _audit("allow", "refreshed",
                           generation=outcome.entry["refresh_generation"])
                    return ok_response(outcome.entry)
                if isinstance(outcome, refresh_mod.WentStale):
                    _audit("needs-consent", "stale-on-refresh")
                    return await b.needs_consent(sub, vendor, spec, want)
                if isinstance(outcome, refresh_mod.VendorDown):
                    _audit("deny", "vendor-unavailable", error=outcome.error)
                    return problem(503, "vendor-unavailable", outcome.error)
                # CasLost: another writer won (§8) — serve theirs, never ours.
                found = await b.get_entry(vendor, sub)
                _audit("allow", "cas-lost")
                if found and found[0]["state"] == "ACTIVE":
                    return ok_response(found[0])
                return problem(503, "vendor-unavailable", "refresh race lost; retry")
            finally:
                await b.coord.release_refresh_lock(vendor, sub, lock_token)
        except CustodyUnavailable as exc:
            # §9: fail closed — no grace beyond the in-memory cache TTL.
            _audit("deny", "vault-unavailable", error=str(exc))
            return problem(503, "vault-unavailable", str(exc))
        except CoordinationUnavailable as exc:
            # Refresh path only: cache hits were already served above.
            _audit("deny", "coordination-unavailable", error=str(exc))
            return problem(503, "coordination-unavailable", str(exc))

    @app.get("/v1/authorize/{vendor}")
    async def authorize(vendor: str, txn: str):
        try:
            record = await b.coord.get_txn(txn)
        except CoordinationUnavailable as exc:
            return problem(503, "coordination-unavailable", str(exc))
        if record is None or record["vendor"] != vendor:
            audit("broker.consent.fail", vendor=vendor, reason="bad_txn",
                  security_event=False)
            return problem(400, "invalid-transaction", "unknown or expired transaction")
        spec = b.vendors.get_vendor(vendor)
        if spec is None:
            return problem(404, "unknown-vendor", vendor)

        try:
            eps = await b.vendors.endpoints(vendor)
            creds = await b.vendors.read_client(vendor)
        except vendors_mod.VendorUnavailable as exc:
            audit("broker.consent.fail", vendor=vendor, sub=record["sub"],
                  reason="vendor_unavailable", security_event=False)
            return problem(503, "vendor-unavailable", str(exc))
        except CustodyUnavailable as exc:
            audit("broker.consent.fail", vendor=vendor, sub=record["sub"],
                  reason="vault_unavailable", security_event=False)
            return problem(503, "vault-unavailable", str(exc))
        if not creds or not creds.get("client_id"):
            audit("broker.consent.fail", vendor=vendor, sub=record["sub"],
                  reason="no_client_credential", security_event=False)
            return problem(503, "vendor-unavailable", f"no client credential for {vendor}")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        try:
            await b.coord.put_state(state, {
                "txn_id": txn, "sub": record["sub"], "vendor": vendor,
                "pkce_verifier": verifier, "nonce": secrets.token_urlsafe(16),
                "issuer": eps.get("issuer"), "created_at": time.time(),
                # RFC 9207 §2.4: if the AS advertises iss support, a callback
                # that omits iss is a mix-up signal (see callback).
                "iss_required": bool(
                    eps.get("authorization_response_iss_parameter_supported")),
                "scopes": record["scopes"]})  # ≤ registry ceiling (§4.2)
        except CoordinationUnavailable as exc:
            return problem(503, "coordination-unavailable", str(exc))
        audit("broker.consent.start", sub=record["sub"], vendor=vendor)
        params = {
            "client_id": creds["client_id"],
            "response_type": "code",
            "redirect_uri": f"{cfg.broker_public_url}/v1/callback/{vendor}",
            "scope": " ".join(record["scopes"]),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return RedirectResponse(f"{eps['authorization_endpoint']}?{urlencode(params)}",
                                headers=BROWSER_HEADERS)

    @app.get("/v1/callback/{vendor}")
    async def callback(vendor: str, request: Request):
        q = request.query_params
        state = q.get("state", "")
        try:
            record = await b.coord.peek_state(state)
            if record is None or record["vendor"] != vendor:
                # §4.3/§10: state replay or mismatch is a SECURITY EVENT.
                audit("broker.consent.fail", vendor=vendor,
                      reason="state_invalid_or_replayed", security_event=True)
                return page("Invalid or expired authorization state.", 400)
            # RFC 9207 mix-up defense: strict string comparison against the
            # issuer recorded at transaction creation; applies before any code
            # redemption — and before consumption, so a tampered callback does
            # not burn the state the legitimate one still needs.
            # When the vendor AS advertises
            # authorization_response_iss_parameter_supported (RFC 8414), an
            # authorization response with no iss is itself a mix-up signal
            # (RFC 9207 §2.4) and is rejected exactly like a mismatched one.
            iss = q.get("iss")
            if record["issuer"] is not None and (
                    iss is None and record["iss_required"] or
                    iss is not None and iss != record["issuer"]):
                audit("broker.consent.fail", vendor=vendor, reason="iss_mismatch",
                      security_event=True, iss_present=iss is not None)
                return page("Issuer mismatch.", 400)
            # Single-use consumption BEFORE redemption: exactly one callback
            # per state ever reaches the token endpoint.
            record = await b.coord.consume_state(state)
            if record is None:  # lost a consumption race — treat as replay
                audit("broker.consent.fail", vendor=vendor,
                      reason="state_invalid_or_replayed", security_event=True)
                return page("Invalid or expired authorization state.", 400)
            await b.coord.pop_txn(record["txn_id"])
        except CoordinationUnavailable:
            audit("broker.consent.fail", vendor=vendor, reason="coordination_unavailable",
                  security_event=False)
            return page("Coordination store unavailable.", 503)
        if "error" in q:
            # The raw vendor error goes to the audit line only; the browser
            # gets a constant page (no reflected attacker-controllable value).
            audit("broker.consent.fail", vendor=vendor, sub=record["sub"],
                  reason=q.get("error"), security_event=False)
            return page("Authorization failed.", 400)
        try:
            tok = await b.vendors.exchange_code(
                vendor, q.get("code", ""), record["pkce_verifier"],
                f"{cfg.broker_public_url}/v1/callback/{vendor}")
        except vendors_mod.VendorError as exc:
            audit("broker.consent.fail", vendor=vendor, sub=record["sub"],
                  reason=str(exc), security_event=False)
            return page("Token exchange failed.", 502)
        # The code is spent from here on: nothing below may lose the new grant
        # silently. vendor_user_id is best-effort ("unknown" on any failure).
        vendor_uid = await b.vendors.vendor_user_id(vendor, tok["access_token"])
        entry = entry_from_token_response(tok, 1, vendor_uid, record["scopes"])
        try:
            await b.custody.write(vendor, record["sub"], entry, cas=None)  # re-consent: gen=1
        except CustodyUnavailable:
            # Unstorable: revoke the fresh grant at the vendor (best effort) so
            # it is not orphaned there, then let the user retry the dance.
            try:
                await b.vendors.revoke(vendor, entry)
                revoked = True
            except vendors_mod.VendorError:
                revoked = False
            audit("broker.consent.fail", vendor=vendor, sub=record["sub"],
                  reason="post_redeem_failure", new_grant_revoked=revoked,
                  security_event=False)
            return page("Credential store unavailable.", 503)
        await b.invalidate(vendor, record["sub"])
        audit("broker.consent.complete", sub=record["sub"], vendor=vendor,
              vendor_user_id=vendor_uid)
        return page("Connected — return to your client.")

    @app.delete("/v1/grants/{vendor}/{sub}")
    async def delete_grant(vendor: str, sub: str, request: Request):
        claims, denied = await hub_claims(request)
        if denied is not None:
            return denied
        if claims["sub"] != sub:
            return problem(403, "forbidden", "grants are self-service (sub must match)")
        if b.vendors.get_vendor(vendor) is None:
            return problem(404, "unknown-vendor", vendor)
        try:
            found = await b.custody.read(vendor, sub)
            if found is None:
                return problem(404, "no-grant", "nothing to revoke")
            entry, ver = found
            outcome = "revoked"
            try:
                await b.vendors.revoke(vendor, entry)   # §4.4: revoke at vendor FIRST
            except vendors_mod.RevocationUnsupported:
                outcome = "unsupported"   # vendor offers no revocation: local delete only
            except vendors_mod.VendorUnavailable as exc:
                # Park REVOKE_PENDING for the sweeper. A refresh may have moved
                # the entry since our read: re-read once and park the newer
                # pair (its RT is the one the sweeper must revoke).
                for _ in range(2):
                    try:
                        await b.custody.write(vendor, sub, {**entry, "state": "REVOKE_PENDING"},
                                        cas=ver)
                        break
                    except CasConflict:
                        found = await b.custody.read(vendor, sub)
                        if found is None:
                            return problem(404, "no-grant", "nothing to revoke")
                        entry, ver = found
                else:
                    return problem(503, "vendor-unavailable",
                                   "revocation failed while the grant changed; retry")
                await b.invalidate(vendor, sub)
                audit("broker.revoke", sub=sub, vendor=vendor, outcome="pending",
                      error=str(exc))
                return problem(502, "revoke-pending", "vendor revocation failed; will retry")
            await b.custody.delete(vendor, sub)
            await b.invalidate(vendor, sub)
        except CustodyUnavailable as exc:
            return problem(503, "vault-unavailable", str(exc))
        audit("broker.revoke", sub=sub, vendor=vendor, outcome=outcome,
              hub_jti=claims.get("jti"))
        if outcome == "unsupported":
            return JSONResponse({"revoked": True, "vendor_revocation": "unsupported"})
        return JSONResponse({"revoked": True})

    @app.get("/v1/grants")
    async def list_grants(request: Request):
        claims, denied = await hub_claims(request)
        if denied is not None:
            return denied
        grants = []
        for vendor in b.vendors.registry():
            if b.vendors.get_vendor(vendor) is None:
                continue
            try:
                found = await b.custody.read(vendor, claims["sub"])
            except CustodyUnavailable as exc:
                return problem(503, "vault-unavailable", str(exc))
            if found:
                entry, _ = found
                # REFRESHING is an internal marker (design §8); it never
                # surfaces externally.
                state = "ACTIVE" if entry["state"] == "REFRESHING" else entry["state"]
                grants.append({"vendor": vendor, "state": state,
                               "granted_scopes": entry["granted_scopes"],
                               "vendor_user_id": entry["vendor_user_id"],
                               "created_at": entry["created_at"]})
        return {"grants": grants}

    @app.get("/v1/admin/vendors/{vendor}")
    async def vendor_record(vendor: str, request: Request):
        claims, denied = await hub_claims(request)
        if denied is not None:
            return denied
        groups = claims.get("groups")
        # A list only: `in` on a string claim would be a substring match.
        if not isinstance(groups, list) or cfg.admin_group not in groups:
            audit("broker.admin.deny", sub=claims.get("sub"), vendor=vendor,
                  reason="not_platform_admin")
            return problem(403, "forbidden", f"requires the {cfg.admin_group} group")
        spec = b.vendors.registry().get(vendor)
        if spec is None:
            return problem(404, "unknown-vendor", vendor)
        return {k: v for k, v in spec.items() if "secret" not in k}

    return app
