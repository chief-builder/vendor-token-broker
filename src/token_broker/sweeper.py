"""Maintenance sweeper (design §8): drop abandoned consent records, retry
pending revocations, and proactively refresh entries approaching expiry
(the 5–15 min band; the 0–5 min band is served lazily by resolve).

Multi-replica (ADR-0001): only the leader-lease holder sweeps, and
the interval is jittered ±20% so replicas never thunder together. The
memory backend keeps the lab's fixed interval and is always the leader.
"""
import asyncio
import time

from . import refresh as refresh_mod
from . import vendors as vendors_mod
from .audit import audit
from .coordination import CoordinationUnavailable
from .custody import CustodyUnavailable


async def sweep_once(b) -> None:
    """One maintenance pass over every enabled vendor's entries."""
    await b.coord.cleanup()
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
        await b.invalidate(vendor, sub)
        audit("broker.revoke", sub=sub, vendor=vendor, outcome="revoked",
              path="sweep-retry")
        return
    takeover = refresh_mod.abandoned(entry, b.cfg.refreshing_ttl_s)
    if entry["state"] != "ACTIVE" and not takeover:
        return
    if not takeover:
        remaining = entry["expires_at"] - time.time()
        if not (b.cfg.refresh_buffer_s < remaining <= b.cfg.proactive_refresh_s):
            return
    lock_token = await b.coord.try_refresh_lock(vendor, sub)
    if lock_token is None:
        return  # a resolve is already refreshing this entry
    try:
        found = b.custody.read(vendor, sub)
        if found is None:
            return
        entry, ver = found
        if entry["state"] != "ACTIVE" and \
                not refresh_mod.abandoned(entry, b.cfg.refreshing_ttl_s):
            return
        await refresh_mod.attempt_refresh(b, vendor, sub, entry, ver,
                                          path="proactive")
    finally:
        await b.coord.release_refresh_lock(vendor, sub, lock_token)


async def sweep_loop(b) -> None:
    while True:
        await asyncio.sleep(b.cfg.sweep_interval_s * (1 + b.coord.sweep_jitter()))
        try:
            if not await b.coord.acquire_sweep_lease():
                continue
            await sweep_once(b)
        except CoordinationUnavailable:
            continue  # no lease decision without redis; try again next tick
        except Exception as exc:
            audit("broker.sweep.error", error=str(exc))
