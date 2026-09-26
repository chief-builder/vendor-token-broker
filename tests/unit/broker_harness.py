"""Offline harness for the broker core: a real Broker + app wired to the
CAS custody fake, a scriptable vendor fake, locally-minted hub JWTs, and
either coordination backend (redis via fakeredis). Requests go through
httpx.ASGITransport — no network, no containers.

Uniquely named (not conftest) so bare imports never collide with the
integration suite's modules."""
import asyncio
import json
import time
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from unit_helpers import MemoryCustody, StaticJWKS, make_config, mint_hub_token

from token_broker.coordination import MemoryCoordination, RedisCoordination
from token_broker.hub import HubValidator
from token_broker.main import Broker, create_app

VENDOR = "mockhub"
CEILING = ["issues:read", "issues:write"]
_KEY = None


def hub_key():
    global _KEY
    if _KEY is None:
        _KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _KEY


def make_entry(**overrides) -> dict:
    now = time.time()
    entry = {
        "access_token": "at-0", "refresh_token": "rt-0",
        "expires_at": now + 3600, "granted_scopes": list(CEILING),
        "vendor_user_id": "vu-1", "state": "ACTIVE",
        "refresh_generation": 1, "last_refresh_at": now, "created_at": now,
    }
    entry.update(overrides)
    return entry


class FakeVendors:
    """Stands in for VendorClient. Refresh behavior is scripted per test."""

    def __init__(self, ceiling=CEILING):
        self.spec = {"vendor_id": VENDOR, "scope_ceiling": list(ceiling)}
        self.refresh_calls = 0
        self.refresh_rts: list[str] = []
        self.refresh_error: Exception | None = None
        self.refresh_delay = 0.0
        self.rotate = True
        self.expires_in = 60
        self.on_refresh = None          # optional hook(n) run mid-refresh
        self.revoke_calls = 0
        self.revoked: list[dict] = []
        self.revoke_error: Exception | None = None
        self.on_revoke = None           # optional hook() run inside revoke
        self.endpoints_error: Exception | None = None
        self.client: dict | None = {"client_id": "cid", "client_secret": "secret"}
        self.client_error: Exception | None = None
        self.exchange_error: Exception | None = None
        self.exchange_calls = 0
        self.consent_token: dict | None = None     # override the code-exchange response
        self.refresh_scope: str | None = None      # override the refresh response scope

    def registry(self) -> dict:
        return {VENDOR: self.spec}

    def get_vendor(self, vendor: str):
        return self.spec if vendor == VENDOR else None

    async def endpoints(self, vendor: str) -> dict:
        if self.endpoints_error is not None:
            raise self.endpoints_error
        return {"authorization_endpoint": "http://as.test/authorize",
                "issuer": "http://as.test",
                "authorization_response_iss_parameter_supported": True}

    async def read_client(self, vendor: str) -> dict | None:
        if self.client_error is not None:
            raise self.client_error
        return self.client

    async def exchange_code(self, vendor, code, verifier, redirect_uri) -> dict:
        self.exchange_calls += 1
        if self.exchange_error is not None:
            raise self.exchange_error
        if self.consent_token is not None:
            return dict(self.consent_token)
        return {"access_token": "at-consent", "refresh_token": "rt-consent",
                "expires_in": 3600, "scope": " ".join(self.spec["scope_ceiling"])}

    async def vendor_user_id(self, vendor: str, access_token: str) -> str:
        return "vu-1"

    async def refresh(self, vendor: str, refresh_token: str) -> dict:
        self.refresh_calls += 1
        n = self.refresh_calls
        self.refresh_rts.append(refresh_token)
        if self.refresh_delay:
            await asyncio.sleep(self.refresh_delay)
        if self.on_refresh is not None:
            self.on_refresh(n)
        if self.refresh_error is not None:
            raise self.refresh_error
        scope = self.refresh_scope
        if scope is None:
            scope = " ".join(self.spec["scope_ceiling"])
        tok = {"access_token": f"at-{n}", "expires_in": self.expires_in, "scope": scope}
        if self.rotate:
            tok["refresh_token"] = f"rt-{n}"
        return tok

    async def revoke(self, vendor: str, entry: dict) -> None:
        self.revoke_calls += 1
        self.revoked.append(entry)
        if self.on_revoke is not None:
            self.on_revoke()
        if self.revoke_error is not None:
            raise self.revoke_error


class FakeHubLogin:
    """Stands in for HubLogin. The fake hub logs the browser in as the
    `login_hint` it was sent, unless `login_as` says someone else logs in."""

    AUTHORIZE = "http://hub.test/authorize"

    def __init__(self):
        self.login_as: str | None = None
        self.exchange_error: Exception | None = None
        self.check_error: Exception | None = None

    async def check(self) -> None:
        if self.check_error is not None:
            raise self.check_error

    async def authorization_url(self, *, state, nonce, challenge, login_hint, redirect_uri):
        params = {"state": state, "nonce": nonce, "code_challenge": challenge,
                  "redirect_uri": redirect_uri}
        if login_hint:
            params["login_hint"] = login_hint
        return f"{self.AUTHORIZE}?" + urlencode(params)

    async def exchange(self, *, code, verifier, redirect_uri, nonce):
        if self.exchange_error is not None:
            raise self.exchange_error
        return {"sub": code.removeprefix("code-for-"), "nonce": nonce}


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class Harness:
    def __init__(self, coord: str = "memory", **cfg_overrides):
        self.cfg = make_config(**cfg_overrides)
        self.custody = MemoryCustody()
        self.vendors = FakeVendors()
        if coord == "redis":
            import fakeredis.aioredis
            self.coord = RedisCoordination(
                self.cfg, instance_id="inst-a",
                client=fakeredis.aioredis.FakeRedis(decode_responses=True))
        else:
            self.coord = MemoryCoordination(self.cfg)
        hub = HubValidator(self.cfg, jwks_client=StaticJWKS(hub_key().public_key()))
        self.hub_login = FakeHubLogin()
        self.broker = Broker(self.cfg, custody=self.custody, hub=hub,
                             vendors=self.vendors, coord=self.coord,
                             hub_login=self.hub_login)
        self.app = create_app(self.cfg, broker=self.broker)
        # One browser: keeps the consent binding cookie across legs.
        self.browser = AsyncClient(transport=ASGITransport(app=self.app),
                                   base_url="http://broker.test")

    def token(self, sub: str = "wf-user-1", **claims) -> str:
        return mint_hub_token(hub_key(), "PS256", self.cfg, sub=sub, **claims)

    def put(self, sub: str = "wf-user-1", **entry) -> None:
        self.custody.write_now(VENDOR, sub, make_entry(**entry))

    def stored(self, sub: str = "wf-user-1") -> dict | None:
        found = self.custody.read_now(VENDOR, sub)
        return found[0] if found else None

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app),
                           base_url="http://broker.test")

    async def authorize(self, sub: str = "wf-user-1"):
        """New txn for `sub`, then GET /v1/authorize in the browser."""
        txn = await self.broker.new_txn(sub, VENDOR, list(self.vendors.spec["scope_ceiling"]))
        return await self.browser.get(f"/v1/authorize/{VENDOR}", params={"txn": txn})

    async def hub_callback(self, authorize_response, browser=None):
        """Complete the fake hub login and hit /v1/callback/_hub."""
        q = query(authorize_response.headers["location"])
        who = self.hub_login.login_as or q["login_hint"]
        return await (browser or self.browser).get(
            "/v1/callback/_hub", params={"state": q["state"], "code": f"code-for-{who}"})

    async def start_consent(self, sub: str = "wf-user-1") -> str:
        """Drive authorize and the hub login; return the vendor `state`."""
        r = await self.authorize(sub)
        assert r.status_code in (302, 307), r.text
        r = await self.hub_callback(r)
        assert r.status_code in (302, 307), r.text
        return query(r.headers["location"])["state"]

    async def callback(self, state: str, **params):
        params.setdefault("code", "code-1")
        params.setdefault("iss", "http://as.test")
        return await self.browser.get(f"/v1/callback/{VENDOR}",
                                      params={"state": state, **params})

    async def resolve(self, as_sub: str = "wf-user-1", **body):
        """POST /v1/tokens/resolve as `as_sub` (the hub JWT subject); body
        fields, including an advisory `sub`, go in **body."""
        body.setdefault("vendor", VENDOR)
        body.setdefault("min_ttl_s", 30)
        async with self.client() as c:
            return await c.post("/v1/tokens/resolve", json=body,
                                headers={"Authorization": f"Bearer {self.token(as_sub)}"})


def audit_events(capsys, event: str | None = None) -> list[dict]:
    """Audit records printed to stdout since the last capsys read."""
    events = []
    for line in capsys.readouterr().out.splitlines():
        if line.startswith('{"audit"'):
            events.append(json.loads(line))
    if event is not None:
        events = [e for e in events if e["audit"] == event]
    return events

