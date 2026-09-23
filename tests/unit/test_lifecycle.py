"""Startup verification, custody-token renewal, and /healthz (review H4)."""
import asyncio
import time

import pytest
from broker_harness import Harness, audit_events
from starlette.testclient import TestClient

from token_broker import lifecycle
from token_broker.custody import CustodyTokenRejected, CustodyUnavailable
from token_broker.hub import HubAuthError, HubUnavailable


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch):
    monkeypatch.setattr(lifecycle, "STARTUP_RETRY_S", 0.02)
    monkeypatch.setattr(lifecycle, "MIN_RENEW_INTERVAL_S", 0.01)


def failing(n_failures: int, exc: Exception, result):
    """An async callable that raises `exc` n_failures times, then returns."""
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] <= n_failures:
            raise exc
        return result
    fn.calls = calls
    return fn


# ------------------------------------------------------------ startup

async def test_startup_returns_token_status():
    h = Harness()
    h.custody.token = (7200, True)
    assert await lifecycle.startup_checks(h.broker) == (7200, True)


async def test_rejected_custody_token_aborts_immediately():
    h = Harness(startup_timeout_s=30)
    h.custody.token_error = CustodyTokenRejected()
    started = time.monotonic()
    with pytest.raises(lifecycle.StartupError, match="custody token rejected"):
        await lifecycle.startup_checks(h.broker)
    assert time.monotonic() - started < 1          # no waiting on a dead token


async def test_unreachable_custody_retries_then_aborts():
    h = Harness(startup_timeout_s=1)
    h.custody.token_error = CustodyUnavailable()
    with pytest.raises(lifecycle.StartupError, match="VAULT_ADDR"):
        await lifecycle.startup_checks(h.broker)


async def test_dependencies_that_come_up_late_are_waited_for():
    h = Harness(startup_timeout_s=5)
    h.custody.token_status = failing(3, CustodyUnavailable(), (3600, True))
    hub_check = failing(2, HubUnavailable("hub signing keys unavailable"), None)
    h.broker.hub.check = hub_check
    assert await lifecycle.startup_checks(h.broker) == (3600, True)
    assert h.custody.token_status.calls["n"] == 4 and hub_check.calls["n"] == 3


async def test_unreachable_hub_aborts_after_timeout():
    h = Harness(startup_timeout_s=1)
    h.broker.hub.check = failing(10**6, HubUnavailable("x"), None)
    with pytest.raises(lifecycle.StartupError, match="HUB_JWKS_URI"):
        await lifecycle.startup_checks(h.broker)


async def test_hub_jwks_without_keys_aborts_immediately():
    h = Harness(startup_timeout_s=30)
    h.broker.hub.check = failing(10**6, HubAuthError("no signing keys"), None)
    with pytest.raises(lifecycle.StartupError, match="hub JWKS unusable"):
        await lifecycle.startup_checks(h.broker)


def test_app_refuses_to_start_with_a_rejected_token():
    h = Harness()
    h.custody.token_error = CustodyTokenRejected()
    with pytest.raises(lifecycle.StartupError), TestClient(h.app):
        pass


def test_app_starts_and_serves_health():
    h = Harness()
    h.custody.token = (7200, True)
    with TestClient(h.app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True, "custody": "ok"}


# ------------------------------------------------------------ renewal

async def _run_for(coro, seconds: float):
    task = asyncio.create_task(coro)
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return task


async def test_renewable_token_is_renewed_before_expiry():
    h = Harness()
    h.custody.renew_ttl = 0.2
    await _run_for(lifecycle.renew_loop(h.broker, ttl=0.2, renewable=True), 0.45)
    assert h.custody.renewals >= 2       # renewed at ~0.1s, then again at ~0.2s ...


@pytest.mark.parametrize("ttl,renewable", [(0, False), (0, True), (3600, False)])
async def test_non_expiring_or_non_renewable_token_is_left_alone(ttl, renewable):
    h = Harness()
    task = await _run_for(lifecycle.renew_loop(h.broker, ttl, renewable), 0.05)
    assert task.done() and not task.cancelled()   # returned on its own
    assert h.custody.renewals == 0


async def test_failed_renewal_is_audited_and_retried(capsys):
    h = Harness()
    h.custody.renew_error = CustodyUnavailable()
    await _run_for(lifecycle.renew_loop(h.broker, ttl=1, renewable=True), 0.8)
    assert h.custody.renewals >= 2
    failed = audit_events(capsys, "broker.custody.renew_failed")
    assert failed and failed[0]["error"] == "custody backend unavailable"
    assert "vtb-dev-broker-token" not in str(failed)


# ------------------------------------------------------------ /healthz

async def _health(h):
    async with h.client() as c:
        return await c.get("/healthz")


@pytest.mark.parametrize("token,error,status,custody", [
    ((7200, True), None, 200, "ok"),
    ((0, False), None, 200, "ok"),                              # never expires
    ((300, True), None, 503, "token-expiring"),                 # < 10 min left
    (None, CustodyTokenRejected(), 503, "token-rejected"),
    (None, CustodyUnavailable(), 200, "unreachable"),           # outage: keep serving
])
async def test_health_reflects_the_custody_credential(token, error, status, custody):
    h = Harness()
    h.custody.token, h.custody.token_error = token, error
    r = await _health(h)
    assert r.status_code == status
    assert r.json() == {"ok": status == 200, "custody": custody}


async def test_health_result_is_cached(monkeypatch):
    h = Harness()
    h.custody.token = (7200, True)
    assert (await _health(h)).status_code == 200
    h.custody.token_error = CustodyTokenRejected()
    assert (await _health(h)).status_code == 200            # cached
    monkeypatch.setattr(lifecycle, "HEALTH_CACHE_S", 0)
    assert (await _health(h)).status_code == 503            # re-checked
