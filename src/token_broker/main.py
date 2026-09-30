"""Vendor Token Broker — app factory and the 7-route surface.

Custodian, not issuer (design §1/§10): this service holds no issuer signing
keys and exposes no token-minting or JWKS endpoint — tests/unit/test_routes.py
audits the route table for exactly that on every push. Vendor private_key_jwt
client-assertion keys live in custody as credential material.

The routes stay thin: authentication here, behavior in `resolve`, `consent`,
and `grants`, all operating on the per-process `Broker`.

Run: uvicorn --factory token_broker.main:create_app
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, consent, grants, lifecycle, sweeper
from . import resolve as resolve_mod
from .audit import audit
from .broker import Broker
from .config import Config
from .hub import HubAuthError, HubUnavailable


def create_app(cfg: Config | None = None, broker: Broker | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    b = broker or Broker(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Refuse to serve with a dead custody token or no hub keys (review H4).
        ttl, renewable = await lifecycle.startup_checks(b)
        await b.coord.start(on_invalidate=b.drop_cache)
        tasks = [asyncio.create_task(lifecycle.renew_loop(b, ttl, renewable))]
        if cfg.sweep_interval_s > 0:
            tasks.append(asyncio.create_task(sweeper.sweep_loop(b)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await b.coord.close()

    # No /docs, /redoc, /openapi.json: the served surface is exactly the 7
    # routes (tests/unit/test_routes.py audits app.routes, not the schema).
    app = FastAPI(
        title="vendor-token-broker",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
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
        ok, custody = await lifecycle.custody_health(b)
        return JSONResponse({"ok": ok, "custody": custody}, status_code=200 if ok else 503)

    @app.post("/v1/tokens/resolve")
    async def resolve(request: Request):
        claims, denied = await hub_claims(request)
        if denied is not None:
            return denied
        return await resolve_mod.resolve(b, claims, await request.body())

    @app.get("/v1/authorize/{vendor}")
    async def authorize(vendor: str, txn: str):
        return await consent.authorize(b, vendor, txn)

    @app.get("/v1/callback/{vendor}")
    async def callback(vendor: str, request: Request):
        return await consent.callback(b, vendor, request)

    # {sub:path}: a hub subject may contain "/" (URI-shaped subs).
    @app.delete("/v1/grants/{vendor}/{sub:path}")
    async def delete_grant(vendor: str, sub: str, request: Request):
        claims, denied = await hub_claims(request)
        if denied is not None:
            return denied
        return await grants.delete_grant(b, vendor, sub, claims)

    @app.get("/v1/grants")
    async def list_grants(request: Request):
        claims, denied = await hub_claims(request)
        if denied is not None:
            return denied
        return await grants.list_grants(b, claims)

    @app.get("/v1/admin/vendors/{vendor}")
    async def vendor_record(vendor: str, request: Request):
        claims, denied = await hub_claims(request)
        if denied is not None:
            return denied
        groups = claims.get("groups")
        # A list only: `in` on a string claim would be a substring match.
        if not isinstance(groups, list) or cfg.admin_group not in groups:
            audit(
                "broker.admin.deny",
                sub=claims.get("sub"),
                vendor=vendor,
                reason="not_platform_admin",
            )
            return problem(403, "forbidden", f"requires the {cfg.admin_group} group")
        spec = b.vendors.registry().get(vendor)
        if spec is None:
            return problem(404, "unknown-vendor", vendor)
        return {k: v for k, v in spec.items() if "secret" not in k}

    return app
