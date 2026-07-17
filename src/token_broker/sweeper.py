"""Maintenance sweeper (design §8): drop abandoned consent transactions,
retry pending revocations, and proactively refresh entries approaching
expiry (the 5–15 min band; the 0–5 min band is served lazily by resolve).
"""
import asyncio
import time

from . import vendors as vendors_mod
from .audit import audit
from .custody import CasConflict, CustodyUnavailable


async def sweep_once(b) -> None:
    """One maintenance pass over every enabled vendor's entries."""
    now = time.time()
    for store in (b.txns, b.states):
        for key in [k for k, v in store.items()
                    if now - v["created_at"] > b.cfg.txn_ttl_s]:
            store.pop(key, None)
    for vendor in list(b.stale_events):
        kept = [t for t in b.stale_events[vendor]
                if now - t < b.cfg.mass_stale_window_s]
        if kept:
            b.stale_events[vendor] = kept
        else:
            b.stale_events.pop(vendor, None)

    for vendor in b.vendors.registry():
        if b.vendors.get_vendor(vendor) is None:
            continue
        try:
            subs = b.custody.list_subjects(vendor)
        except CustodyUnavailable:
            continue
        for sub in subs:
            try:
                await sweep_entry(b, vendor, sub)
            except Exception as exc:  # a bad entry must never kill the loop
                audit("broker.sweep.error", vendor=vendor, sub=sub, error=str(exc))


async def sweep_entry(b, vendor: str, sub: str) -> None:
    found = b.custody.read(vendor, sub)
    if found is None:
        return
    entry, ver = found
    if entry["state"] == "REVOKE_PENDING":
        try:
            await b.vendors.revoke(vendor, entry)
        except vendors_mod.VendorUnavailable:
            return  # still down; retry next pass
        b.custody.delete(vendor, sub)
        b.drop_cache(vendor, sub)
        audit("broker.revoke", sub=sub, vendor=vendor, outcome="revoked",
              path="sweep-retry")
        return
    if entry["state"] != "ACTIVE":
        return
    remaining = entry["expires_at"] - time.time()
    if not (b.cfg.refresh_buffer_s < remaining <= b.cfg.proactive_refresh_s):
        return
    lock = b.lock(vendor, sub)
    if lock.locked():
        return  # a resolve is already refreshing this entry
    await lock.acquire()
    try:
        found = b.custody.read(vendor, sub)
        if found is None or found[0]["state"] != "ACTIVE":
            return
        entry, ver = found
        try:
            tok = await b.vendors.refresh(vendor, entry["refresh_token"])
        except vendors_mod.InvalidGrant:
            try:
                b.custody.write(vendor, sub, {**entry, "state": "STALE"}, cas=ver)
            except CasConflict:
                return
            b.drop_cache(vendor, sub)
            b.mark_stale(vendor, sub, entry["refresh_generation"])
            return
        except vendors_mod.VendorUnavailable:
            return
        from .main import entry_from_token_response
        new_gen = entry["refresh_generation"] + 1
        new_entry = entry_from_token_response(
            tok, new_gen, entry["vendor_user_id"], entry["granted_scopes"])
        if not new_entry["refresh_token"]:
            new_entry["refresh_token"] = entry["refresh_token"]
        try:
            new_ver = b.custody.write(vendor, sub, new_entry, cas=ver)
        except CasConflict:
            b.drop_cache(vendor, sub)
            return
        b.put_cache(vendor, sub, new_entry, new_ver)
        audit("broker.refresh", sub=sub, vendor=vendor, path="proactive",
              generation_from=entry["refresh_generation"], generation_to=new_gen)
    finally:
        lock.release()


async def sweep_loop(b) -> None:
    while True:
        await asyncio.sleep(b.cfg.sweep_interval_s)
        try:
            await sweep_once(b)
        except Exception as exc:
            audit("broker.sweep.error", error=str(exc))
