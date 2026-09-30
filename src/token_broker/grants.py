"""Self-service grant management (design §4.4): DELETE /v1/grants revokes
at the vendor first and then deletes custody (or parks REVOKE_PENDING for
the sweeper when the vendor is down); GET /v1/grants lists the caller's
grants."""

from fastapi.responses import JSONResponse

from . import vendors as vendors_mod
from .audit import audit
from .broker import Broker
from .coordination import CoordinationUnavailable
from .custody import CasConflict, CustodyUnavailable


async def delete_grant(b: Broker, vendor: str, sub: str, claims: dict) -> JSONResponse:
    """Revoke and delete the caller's own grant for `vendor`."""
    problem = b.problem
    if claims["sub"] != sub:
        return problem(403, "forbidden", "grants are self-service (sub must match)")
    if b.vendors.get_vendor(vendor) is None:
        return problem(404, "unknown-vendor", vendor)
    # Hold the per-entry refresh lock so no refresh can rotate the pair
    # between the vendor revoke and the custody delete (review L4).
    try:
        lock_token, _ = await b.coord.wait_refresh_lock(vendor, sub, None)
    except CoordinationUnavailable as exc:
        return problem(503, "coordination-unavailable", str(exc))
    if lock_token is None:
        return problem(503, "vendor-unavailable", "grant is being refreshed; retry")
    try:
        return await revoke_and_delete(b, vendor, sub, claims)
    except CustodyUnavailable as exc:
        return problem(503, "vault-unavailable", str(exc))
    finally:
        await b.coord.release_refresh_lock(vendor, sub, lock_token)


async def revoke_and_delete(b: Broker, vendor: str, sub: str, claims: dict) -> JSONResponse:
    problem = b.problem
    found = await b.custody.read(vendor, sub)
    if found is None:
        return problem(404, "no-grant", "nothing to revoke")
    entry, ver = found
    outcome = "revoked"
    # Revoke at the vendor FIRST (§4.4). The lock keeps the entry still,
    # but a lock can be lost (redis TTL): if a newer pair landed after our
    # revoke, revoke that one too before deleting.
    for _ in range(3):
        try:
            await b.vendors.revoke(vendor, entry)
        except vendors_mod.RevocationUnsupported:
            outcome = "unsupported"  # vendor offers no revocation: local delete only
        except vendors_mod.VendorUnavailable as exc:
            return await park_revoke_pending(b, vendor, sub, entry, ver, exc)
        current = await b.custody.read(vendor, sub)
        if current is None or current[1] == ver:
            break
        entry, ver = current
    else:
        return problem(503, "vendor-unavailable", "grant kept changing during revocation; retry")
    if current is not None:
        await b.custody.delete(vendor, sub)
    await b.invalidate(vendor, sub)
    audit("broker.revoke", sub=sub, vendor=vendor, outcome=outcome, hub_jti=claims.get("jti"))
    if outcome == "unsupported":
        return JSONResponse({"revoked": True, "vendor_revocation": "unsupported"})
    return JSONResponse({"revoked": True})


async def park_revoke_pending(
    b: Broker, vendor: str, sub: str, entry: dict, ver: int, exc: Exception
) -> JSONResponse:
    """Vendor down: park REVOKE_PENDING for the sweeper. If the entry moved
    since our read, re-read once and park the newer pair (its refresh
    token is the one the sweeper must revoke)."""
    problem = b.problem
    for _ in range(2):
        try:
            await b.custody.write(vendor, sub, {**entry, "state": "REVOKE_PENDING"}, cas=ver)
            break
        except CasConflict:
            found = await b.custody.read(vendor, sub)
            if found is None:
                return problem(404, "no-grant", "nothing to revoke")
            entry, ver = found
    else:
        return problem(
            503, "vendor-unavailable", "revocation failed while the grant changed; retry"
        )
    await b.invalidate(vendor, sub)
    audit("broker.revoke", sub=sub, vendor=vendor, outcome="pending", error=str(exc))
    return problem(502, "revoke-pending", "vendor revocation failed; will retry")


async def list_grants(b: Broker, claims: dict) -> JSONResponse | dict:
    """The caller's grants across enabled vendors (no token material)."""
    problem = b.problem
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
            grants.append(
                {
                    "vendor": vendor,
                    "state": state,
                    "granted_scopes": entry["granted_scopes"],
                    "vendor_user_id": entry["vendor_user_id"],
                    "created_at": entry["created_at"],
                }
            )
    return {"grants": grants}
