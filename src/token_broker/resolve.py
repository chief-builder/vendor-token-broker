"""POST /v1/tokens/resolve (design §4.1, §8, §9): hand the caller a live
vendor token, refreshing it single-flight when it is inside the refresh
buffer, or answer with the consent link / problem that tells the caller what
to do instead."""

import json
import time

from fastapi.responses import JSONResponse

from . import refresh as refresh_mod
from .audit import audit
from .broker import Broker
from .coordination import CoordinationUnavailable
from .custody import CustodyUnavailable


def consent_scopes(required: list[str], ceiling: list[str]) -> list[str]:
    """§4.1/§6: request the minimum — the tool's required scopes capped by the
    registry ceiling. No required_scopes (legacy caller) → the full ceiling."""
    if not required:
        return list(ceiling)
    return [s for s in required if s in ceiling]


def reconsent_scopes(held: list[str], required: list[str], ceiling: list[str]) -> list[str]:
    """§4.1: re-consent for the union of held and required scopes, capped by
    the ceiling (ceiling order preserved)."""
    return [s for s in ceiling if s in set(held) | set(required)]


class InvalidRequest(ValueError):
    """The resolve body is malformed (400 invalid-request); the message says why."""


def parse_resolve_body(raw: bytes) -> dict:
    """The validated resolve request body; raises InvalidRequest."""
    try:
        body = json.loads(raw)
    except ValueError:
        raise InvalidRequest("body must be JSON") from None
    if not isinstance(body, dict):
        raise InvalidRequest("body must be a JSON object")
    if not isinstance(body.get("vendor"), str) or not body["vendor"]:
        raise InvalidRequest("vendor must be a non-empty string")
    if body.get("sub") is not None and not isinstance(body["sub"], str):
        raise InvalidRequest("sub must be a string")
    ttl = body.get("min_ttl_s", 120)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
        raise InvalidRequest("min_ttl_s must be a non-negative integer")
    scopes = body.get("required_scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(x, str) for x in scopes):
        raise InvalidRequest("required_scopes must be a list of strings")
    return body


def ok_response(entry: dict) -> JSONResponse:
    return JSONResponse(
        {
            "access_token": entry["access_token"],
            "expires_at": entry["expires_at"],
            "granted_scopes": entry["granted_scopes"],
        }
    )


async def resolve(b: Broker, claims: dict, raw: bytes) -> JSONResponse:
    """Resolve for the caller whose hub JWT produced `claims`."""
    cfg, problem = b.cfg, b.problem
    try:
        body = parse_resolve_body(raw)
    except InvalidRequest as invalid:
        audit(
            "broker.resolve",
            decision="deny",
            reason="invalid_request",
            hub_jti=claims.get("jti"),
            sub=claims["sub"],
        )
        return problem(400, "invalid-request", str(invalid))
    vendor = body["vendor"]
    sub = claims["sub"]  # the JWT is authoritative; the field is advisory
    if body.get("sub") and body["sub"] != sub:
        audit(
            "broker.resolve",
            decision="deny",
            reason="sub_mismatch",
            hub_jti=claims.get("jti"),
            sub=sub,
            claimed_sub=body["sub"],
            vendor=vendor,
        )
        return problem(400, "sub-mismatch", "request sub does not match hub token")
    # min_ttl_s is honored up to REFRESH_BUFFER_S: beyond it a caller
    # could force a vendor refresh on every call for vendors whose tokens
    # are shorter than the requested TTL (review M4). Clamps are audited.
    requested_ttl = body.get("min_ttl_s", 120)
    min_ttl = min(requested_ttl, cfg.refresh_buffer_s)
    clamp = {"min_ttl_clamped_from": requested_ttl} if requested_ttl > min_ttl else {}
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
        # A caller can never widen past the registry ceiling (§4.1).
        audit(
            "broker.resolve",
            decision="deny",
            path="scope-ceiling",
            hub_jti=claims.get("jti"),
            sub=sub,
            vendor=vendor,
            required=required,
            ceiling=ceiling,
        )
        return problem(
            403,
            "scope-exceeds-ceiling",
            "required_scopes exceed the vendor registry ceiling",
            required=required,
            ceiling=ceiling,
        )
    want = consent_scopes(required, ceiling)

    def _audit(decision: str, path: str, **kw):
        audit(
            "broker.resolve",
            decision=decision,
            path=path,
            hub_jti=claims.get("jti"),
            sub=sub,
            vendor=vendor,
            **clamp,
            **kw,
        )

    async def _insufficient_scope(entry: dict) -> JSONResponse | None:
        """§4.1: an ACTIVE grant that doesn't cover the tool's required
        scopes returns 409 needs-reconsent-scope with an authorize_uri that
        re-consents for the union of held and required scopes (≤ ceiling)."""
        missing = [s for s in required if s not in entry["granted_scopes"]]
        if not missing:
            return None
        txn = await b.new_txn(
            sub, vendor, reconsent_scopes(entry["granted_scopes"], required, ceiling)
        )
        _audit("deny", "insufficient-scope", missing=missing)
        return problem(
            409,
            "needs-reconsent-scope",
            "grant does not cover the required scopes",
            missing_scopes=missing,
            authorize_uri=f"{cfg.broker_public_url}/v1/authorize/{vendor}?txn={txn}",
        )

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

        def _serve_winner(e: dict) -> JSONResponse | None:
            """Once another writer advanced the generation, its token is
            as fresh as the vendor issues: serve it even if shorter than
            min_ttl. Refreshing again cannot produce a longer-lived token
            and would only burn vendor calls (review M4)."""
            if e["state"] != "ACTIVE" or e["refresh_generation"] == gen_before:
                return None
            remaining = e["expires_at"] - time.time()
            if remaining <= 0:
                return None
            short = {"short_ttl": True} if remaining < min_ttl else {}
            _audit("allow", "refresh-waited", generation=e["refresh_generation"], **short)
            return ok_response(e)

        async def _serve_if_gen_advanced():
            """Waiter retry check (redis profile): re-read each retry and
            serve without the lock if another replica's refresh already
            advanced the generation (ADR-0001)."""
            b.drop_cache(vendor, sub)
            f = await b.get_entry(vendor, sub)
            return _serve_winner(f[0]) if f else None

        lock_token, early = await b.coord.wait_refresh_lock(vendor, sub, _serve_if_gen_advanced)
        if early is not None:
            return early
        if lock_token is None:
            # Lock-holder death path: re-read and serve if usable (§8 rules).
            b.drop_cache(vendor, sub)
            found = await b.get_entry(vendor, sub)
            if (
                found
                and found[0]["state"] == "ACTIVE"
                and found[0]["expires_at"] - time.time() >= min_ttl
            ):
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
            if entry["state"] == "REVOKE_PENDING":
                # A delete parked it while we waited: never refresh it
                # back to ACTIVE (that would undo the pending revocation).
                _audit("deny", "revoke-pending")
                return problem(409, "revoke-pending", "entry is being revoked")
            if entry["state"] == "REFRESHING" and not refresh_mod.abandoned(
                entry, cfg.refreshing_ttl_s
            ):
                # In flight on another replica whose lock TTL'd out from
                # under it. Never re-refresh (a rotating RT would burn the
                # family) — serve the still-valid token or ask for a retry.
                if entry["expires_at"] - time.time() >= min_ttl:
                    _audit("allow", "refresh-in-progress")
                    return ok_response(entry)
                _audit("deny", "refresh-in-progress")
                return problem(503, "vendor-unavailable", "refresh in progress; retry")
            winner = _serve_winner(entry)  # a concurrent refresh already won
            if winner is not None:
                return winner

            outcome = await refresh_mod.attempt_refresh(b, vendor, sub, entry, ver)
            if isinstance(outcome, refresh_mod.Refreshed):
                _audit("allow", "refreshed", generation=outcome.entry["refresh_generation"])
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
