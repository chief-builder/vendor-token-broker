"""Per-process broker state: the collaborators (custody, hub, vendors,
coordination, hub login), the per-replica token cache, and the consent-link
and STALE bookkeeping that the routes and the sweeper share."""

import secrets
import time

from fastapi.responses import JSONResponse

from .audit import audit
from .config import Config
from .coordination import CoordinationUnavailable, make_coordination
from .custody import VaultStore
from .hub import HubValidator
from .hub_login import HubLogin
from .problems import Problems
from .vendors import VendorClient

# Per-replica token cache bound. Entries also expire after CACHE_TTL_S;
# expired ones are purged at most once per TTL, so token material for users
# who never come back does not linger in memory (review L6).
CACHE_MAX_ENTRIES = 10_000


class Broker:
    """All per-process broker state; routes and the sweeper operate on this."""

    def __init__(
        self, cfg: Config, *, custody=None, hub=None, vendors=None, coord=None, hub_login=None
    ):
        self.cfg = cfg
        self.instance_id = secrets.token_hex(8)
        self.problem = Problems(cfg.problem_urn_prefix)
        self.custody = custody or VaultStore(cfg)
        self.hub = hub or HubValidator(cfg)
        self.vendors = vendors or VendorClient(cfg, self.custody)
        self.coord = coord or make_coordination(cfg, self.instance_id)
        self.hub_login = hub_login or HubLogin(cfg, self.hub)
        self.cache: dict[tuple[str, str], tuple[dict, int, float]] = {}
        self._cache_pruned_at = time.time()
        self.sweep_cursor = 0  # where the next budgeted sweep pass resumes
        self.health_cache: tuple[float, tuple[bool, str]] | None = None

    async def new_txn(self, sub: str, vendor: str, scopes: list[str]) -> str:
        txn_id = secrets.token_urlsafe(24)
        await self.coord.put_txn(
            txn_id, {"sub": sub, "vendor": vendor, "scopes": scopes, "created_at": time.time()}
        )
        return txn_id

    async def needs_consent(
        self, sub: str, vendor: str, spec: dict, scopes: list[str] | None = None
    ) -> JSONResponse:
        txn = await self.new_txn(
            sub, vendor, scopes if scopes is not None else spec.get("scope_ceiling", [])
        )
        return self.problem(
            404,
            "needs-consent",
            f"no usable grant for {vendor}",
            authorize_uri=f"{self.cfg.broker_public_url}/v1/authorize/{vendor}?txn={txn}",
        )

    async def get_entry(self, vendor: str, sub: str):
        key = (vendor, sub)
        cached = self.cache.get(key)
        if cached and time.time() - cached[2] < self.cfg.cache_ttl_s:
            return cached[0], cached[1]
        found = await self.custody.read(vendor, sub)
        if found is None:
            self.cache.pop(key, None)
            return None
        self.put_cache(vendor, sub, found[0], found[1])
        return found

    def put_cache(self, vendor: str, sub: str, entry: dict, ver: int) -> None:
        now = time.time()
        if now - self._cache_pruned_at >= self.cfg.cache_ttl_s:
            self._cache_pruned_at = now
            for key in [k for k, v in self.cache.items() if now - v[2] >= self.cfg.cache_ttl_s]:
                del self.cache[key]
        self.cache.pop((vendor, sub), None)  # re-insert: newest last
        self.cache[(vendor, sub)] = (entry, ver, now)
        while len(self.cache) > CACHE_MAX_ENTRIES:  # evict the oldest insert
            del self.cache[next(iter(self.cache))]

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

    async def mark_stale(
        self, vendor: str, sub: str, generation: int, anomaly: bool = True
    ) -> None:
        """Emit the per-entry broker.stale record and, when STALEs for one
        vendor burst past the threshold within the window, a mass-stale page
        (§8/§9 — the org-App-uninstall anomaly). anomaly=False (a token that
        simply ran out) is recorded but never counted toward the page."""
        audit("broker.stale", sub=sub, vendor=vendor, generation=generation)
        if not anomaly:
            return
        try:
            count, page = await self.coord.record_stale(vendor)
        except CoordinationUnavailable:
            return  # the per-entry record above still stands
        if page:
            audit(
                "broker.stale.mass",
                vendor=vendor,
                count=count,
                window_s=self.cfg.mass_stale_window_s,
                page=True,
                security_event=True,
            )
