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


def entry_from_token_response(tok: dict, gen: int, vendor_uid: str,
                              scopes: list[str]) -> dict:
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": time.time() + float(tok.get("expires_in", 8 * 3600)),
        "granted_scopes": (tok.get("scope") or " ".join(scopes)).split(),
        "vendor_user_id": vendor_uid,
        "state": "ACTIVE",
        "refresh_generation": gen,
        "last_refresh_at": time.time(),
        "created_at": time.time(),
    }


def abandoned(entry: dict, refreshing_ttl_s: int) -> bool:
    """A persisted REFRESHING whose owner has been silent past the TTL is
    abandoned — the next lock holder takes over (design §8)."""
    return (entry.get("state") == "REFRESHING"
            and time.time() - float(entry.get("refresh_started_at", 0))
            >= refreshing_ttl_s)


async def attempt_refresh(b, vendor: str, sub: str, entry: dict, ver: int,
                          *, path: str | None = None):
    """Refresh `entry` (which the caller re-read under the lock) and CAS-write
    the outcome. Returns Refreshed | WentStale | VendorDown | CasLost."""
    gen_from = entry["refresh_generation"]

    if b.coord.persist_refreshing:
        marker = {**entry, "state": "REFRESHING",
                  "refresh_owner": b.instance_id,
                  "refresh_started_at": time.time()}
        try:
            ver = b.custody.write(vendor, sub, marker, cas=ver)
        except CasConflict:
            b.drop_cache(vendor, sub)
            return CasLost()
        entry = marker

    try:
        tok = await b.vendors.refresh(vendor, entry["refresh_token"])
    except vendors_mod.InvalidGrant:
        try:
            b.custody.write(vendor, sub, {**entry, "state": "STALE"}, cas=ver)
        except CasConflict:
            pass
        await b.invalidate(vendor, sub)
        await b.mark_stale(vendor, sub, gen_from)
        return WentStale(gen_from)
    except vendors_mod.VendorUnavailable as exc:
        if b.coord.persist_refreshing:
            # Known failure: restore ACTIVE rather than waiting out the
            # abandoned-REFRESHING takeover TTL.
            try:
                b.custody.write(vendor, sub, {**entry, "state": "ACTIVE"}, cas=ver)
            except CasConflict:
                pass
            b.drop_cache(vendor, sub)
        return VendorDown(str(exc))

    new_gen = gen_from + 1
    new_entry = entry_from_token_response(
        tok, new_gen, entry["vendor_user_id"], entry["granted_scopes"])
    if not new_entry["refresh_token"]:
        new_entry["refresh_token"] = entry["refresh_token"]  # non-rotating vendor
    try:
        new_ver = b.custody.write(vendor, sub, new_entry, cas=ver)
    except CasConflict:
        # Another writer won (§8): discard our pair, never write the older one.
        b.drop_cache(vendor, sub)
        return CasLost()
    b.put_cache(vendor, sub, new_entry, new_ver)
    extra = {"path": path} if path else {}
    audit("broker.refresh", sub=sub, vendor=vendor, **extra,
          generation_from=gen_from, generation_to=new_gen)
    return Refreshed(new_entry, new_ver)
