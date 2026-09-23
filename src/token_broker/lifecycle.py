"""Startup verification, custody-token renewal, and service health.

- Startup: the broker's custody token and the hub JWKS are checked before
  serving. Unreachable dependencies are retried for STARTUP_TIMEOUT_S (so a
  replica may start alongside them); a rejected custody token or an unusable
  JWKS aborts at once, because waiting cannot fix it.
- Renewal: a renewable token with a TTL is renewed at half its remaining
  TTL. A failed renewal is audited and retried with backoff. A token with a
  hard maximum TTL can only be renewed up to that maximum; use a periodic
  token (docs/operations.md) for indefinite service.
- Health: /healthz fails only for problems local to this replica's custody
  credential (rejected, or expiring within TOKEN_EXPIRY_WARN_S). A custody
  OUTAGE is reported but stays healthy: every replica shares it, and
  draining or restarting them would discard the cache hits that keep
  serving during the outage (design §9).
"""
import asyncio
import time

from .audit import audit
from .custody import CustodyTokenRejected, CustodyUnavailable
from .hub import HubAuthError, HubUnavailable

STARTUP_RETRY_S = 1.0
MIN_RENEW_INTERVAL_S = 1.0
MAX_RENEW_BACKOFF_S = 300.0
HEALTH_CACHE_S = 10.0
TOKEN_EXPIRY_WARN_S = 600


class StartupError(Exception):
    pass


async def startup_checks(b) -> tuple[int, bool]:
    """Verify custody token and hub JWKS; return (token ttl, renewable)."""
    deadline = time.monotonic() + b.cfg.startup_timeout_s
    status, hub_ok = None, False
    while True:
        waiting = []
        if status is None:
            try:
                status = await b.custody.token_status()
            except CustodyTokenRejected as exc:
                raise StartupError(
                    "custody token rejected: check VAULT_TOKEN / VAULT_TOKEN_FILE "
                    "and that the token carries the broker and default policies") from exc
            except CustodyUnavailable:
                waiting.append("custody backend unreachable at VAULT_ADDR")
        if not hub_ok:
            try:
                await b.hub.check()
                hub_ok = True
            except HubUnavailable:
                waiting.append("hub JWKS unreachable at HUB_JWKS_URI")
            except HubAuthError as exc:
                raise StartupError(f"hub JWKS unusable: {exc}") from exc
        if not waiting:
            return status
        if time.monotonic() >= deadline:
            raise StartupError(
                f"startup checks failed after {b.cfg.startup_timeout_s}s: " + "; ".join(waiting))
        await asyncio.sleep(STARTUP_RETRY_S)


async def renew_loop(b, ttl: int, renewable: bool) -> None:
    """Keep the custody token alive: renew at half the remaining TTL."""
    if ttl <= 0 or not renewable:
        return   # never expires, or cannot be renewed (health will warn)
    expires = time.monotonic() + ttl
    delay = max(ttl / 2, MIN_RENEW_INTERVAL_S)
    backoff = MIN_RENEW_INTERVAL_S
    while True:
        await asyncio.sleep(delay)
        try:
            ttl = await b.custody.renew_token()
        except CustodyUnavailable as exc:
            remaining = expires - time.monotonic()
            audit("broker.custody.renew_failed", error=str(exc),
                  ttl_remaining_s=max(0, int(remaining)))
            delay = max(min(backoff, remaining / 2), MIN_RENEW_INTERVAL_S)
            backoff = min(backoff * 2, MAX_RENEW_BACKOFF_S)
            continue
        if ttl <= 0:
            return
        expires = time.monotonic() + ttl
        delay = max(ttl / 2, MIN_RENEW_INTERVAL_S)
        backoff = MIN_RENEW_INTERVAL_S


async def custody_health(b) -> tuple[bool, str]:
    """(healthy, custody status), cached for HEALTH_CACHE_S so probes do not
    load the backend. Status: ok | unreachable | token-expiring | token-rejected."""
    now = time.monotonic()
    if b.health_cache is not None and now - b.health_cache[0] < HEALTH_CACHE_S:
        return b.health_cache[1]
    try:
        ttl, _ = await b.custody.token_status()
        result = (False, "token-expiring") if 0 < ttl < TOKEN_EXPIRY_WARN_S else (True, "ok")
    except CustodyTokenRejected:
        result = (False, "token-rejected")
    except CustodyUnavailable:
        result = (True, "unreachable")
    b.health_cache = (now, result)
    return result
