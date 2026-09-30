"""The single-flight refresh core (design §8/§9), shared by the resolve
path and the proactive sweeper.

The caller holds the per-entry coordination lock and passes the entry it
just re-read. This module performs the vendor call and the generation-CAS
outcome write. On the redis profile it first persists `state=REFRESHING`
(+ owner + started_at) so other replicas can distinguish in-flight from
stale (design §8); REFRESHING never surfaces externally.

The KV-v2 CAS is the correctness backstop everywhere: a lost lock can never
write a stale token pair — the CAS loser discards its result, re-reads, and
never writes the older pair.
"""

import time
from dataclasses import dataclass

from . import vendors as vendors_mod
from .audit import audit
from .custody import CasConflict


@dataclass
class Refreshed:
    entry: dict
    ver: int


@dataclass
class WentStale:
    generation: int


@dataclass
class VendorDown:
    error: str


@dataclass
class CasLost:
    pass


# A token response with neither expires_in nor a refresh token describes a
# non-expiring token (e.g. a GitHub App user token with expiry disabled).
# It is stored with a far-future expiry so resolve and the sweeper never try
# to refresh it; it lives until revoked.
NON_EXPIRING_S = 10 * 365 * 86400
DEFAULT_EXPIRES_IN_S = 8 * 3600  # refreshable token that omits expires_in


def granted_scopes(tok: dict, scopes: list[str], ceiling: list[str] | None) -> list[str]:
    """The scopes to record: the vendor's answer (or what was asked for),
    capped by a non-empty registry ceiling."""
    granted = (tok.get("scope") or " ".join(scopes)).split()
    return [s for s in granted if s in ceiling] if ceiling else granted


def scope_widening(tok: dict, ceiling: list[str] | None) -> list[str]:
    """Scopes the vendor granted beyond the ceiling (dropped from the entry)."""
    if not ceiling:
        return []
    return [s for s in (tok.get("scope") or "").split() if s not in ceiling]


def entry_from_token_response(
    tok: dict,
    gen: int,
    vendor_uid: str,
    scopes: list[str],
    created_at: float | None = None,
    previous_refresh_token: str = "",
    ceiling: list[str] | None = None,
) -> dict:
    """Build a custody entry. `created_at` is the consent time: pass the
    existing entry's value on refresh; None (a new consent) means now.
    `previous_refresh_token` is kept when a non-rotating vendor returns none."""
    now = time.time()
    refresh_token = tok.get("refresh_token") or previous_refresh_token
    if "expires_in" in tok:
        expires_at = now + float(tok["expires_in"])
    elif refresh_token:
        expires_at = now + DEFAULT_EXPIRES_IN_S
    else:
        expires_at = now + NON_EXPIRING_S
    return {
        "access_token": tok["access_token"],
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "granted_scopes": granted_scopes(tok, scopes, ceiling),
        "vendor_user_id": vendor_uid,
        "state": "ACTIVE",
        "refresh_generation": gen,
        "last_refresh_at": now,
        "created_at": now if created_at is None else created_at,
    }


def abandoned(entry: dict, refreshing_ttl_s: int) -> bool:
    """A persisted REFRESHING whose owner has been silent past the TTL is
    abandoned — the next lock holder takes over (design §8)."""
    return (
        entry.get("state") == "REFRESHING"
        and time.time() - float(entry.get("refresh_started_at", 0)) >= refreshing_ttl_s
    )


async def attempt_refresh(
    b, vendor: str, sub: str, entry: dict, ver: int, *, path: str | None = None
):
    """Refresh `entry` (which the caller re-read under the lock) and CAS-write
    the outcome. Returns Refreshed | WentStale | VendorDown | CasLost."""
    gen_from = entry["refresh_generation"]

    if not entry.get("refresh_token"):
        # Nothing to refresh with: never send an empty refresh token to the
        # vendor. The token simply ran out; the user re-consents. Not an
        # uninstall signal, so it does not count toward mass-STALE.
        return await _go_stale(b, vendor, sub, entry, ver, gen_from, anomaly=False)

    if b.coord.persist_refreshing:
        marker = {
            **entry,
            "state": "REFRESHING",
            "refresh_owner": b.instance_id,
            "refresh_started_at": time.time(),
        }
        try:
            ver = await b.custody.write(vendor, sub, marker, cas=ver)
        except CasConflict:
            b.drop_cache(vendor, sub)
            return CasLost()
        entry = marker

    try:
        tok = await b.vendors.refresh(vendor, entry["refresh_token"])
    except vendors_mod.InvalidGrant:
        return await _go_stale(b, vendor, sub, entry, ver, gen_from, anomaly=True)
    except vendors_mod.VendorUnavailable as exc:
        if b.coord.persist_refreshing:
            # Known failure: restore ACTIVE rather than waiting out the
            # abandoned-REFRESHING takeover TTL.
            try:
                await b.custody.write(vendor, sub, {**entry, "state": "ACTIVE"}, cas=ver)
            except CasConflict:
                pass
            b.drop_cache(vendor, sub)
        return VendorDown(str(exc))

    new_gen = gen_from + 1
    ceiling = b.vendors.registry().get(vendor, {}).get("scope_ceiling")
    new_entry = entry_from_token_response(
        tok,
        new_gen,
        entry["vendor_user_id"],
        entry["granted_scopes"],
        created_at=entry.get("created_at"),
        previous_refresh_token=entry["refresh_token"],  # non-rotating vendor
        ceiling=ceiling,
    )
    try:
        new_ver = await b.custody.write(vendor, sub, new_entry, cas=ver)
    except CasConflict:
        # Another writer won (§8): discard our pair, never write the older one.
        b.drop_cache(vendor, sub)
        return CasLost()
    b.put_cache(vendor, sub, new_entry, new_ver)
    extra: dict[str, object] = {"path": path} if path else {}
    widened = scope_widening(tok, ceiling)
    if widened:
        extra["scope_widened"] = widened
    audit(
        "broker.refresh",
        sub=sub,
        vendor=vendor,
        **extra,
        generation_from=gen_from,
        generation_to=new_gen,
    )
    return Refreshed(new_entry, new_ver)


async def _go_stale(
    b, vendor: str, sub: str, entry: dict, ver: int, gen_from: int, *, anomaly: bool
):
    """CAS-write STALE with the dead token material blanked (a STALE entry
    is only ever re-consented over or deleted, never used); on a lost CAS,
    report the race instead."""
    scrubbed = {**entry, "state": "STALE", "access_token": "", "refresh_token": ""}
    try:
        await b.custody.write(vendor, sub, scrubbed, cas=ver)
    except CasConflict:
        # Another writer moved the entry (a concurrent refresh or a
        # re-consent): this failure is about a superseded pair, so neither
        # mark STALE nor count toward the mass-STALE window.
        b.drop_cache(vendor, sub)
        return CasLost()
    await b.invalidate(vendor, sub)
    await b.mark_stale(vendor, sub, gen_from, anomaly=anomaly)
    return WentStale(gen_from)
