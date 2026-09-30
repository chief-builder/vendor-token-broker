"""Integration helpers: drive the tests/stack compose stack — hub-stub
mints the workforce tokens, mockhub is the vendor, OpenBao is custody.
Fixtures live in conftest.py; this module is uniquely named so bare imports
never collide with the unit suite's modules.

Ported from the source lab's phase5/phase7 conftests, with Keycloak logins
replaced by hub-stub minting.
"""

import base64
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager

import pytest
import requests

# The multi profile points BROKER at the LB and lists both replicas via env.
BROKER = os.environ.get("BROKER_URL", "http://localhost:8300")
MOCK = "http://localhost:8310"
HUB = "http://localhost:8320"
BROKER_CONTAINERS = os.environ.get("BROKER_CONTAINERS", "vtb-broker").split(",")
OPENBAO_CONTAINER = "vtb-openbao"
MOCK_CONTAINER = "vtb-mock-vendor"


def mint(sub: str = "wf-user-1", kind: str = "ps256", **extra) -> str:
    r = requests.post(f"{HUB}/_test/token", json={"sub": sub, "kind": kind, **extra}, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


def claims_of(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def sub_of(token: str) -> str:
    return claims_of(token)["sub"]


def resolve(
    token: str,
    vendor: str = "mockhub",
    min_ttl_s: int = 30,
    sub: str | None = None,
    required_scopes: list[str] | None = None,
) -> requests.Response:
    body: dict = {"vendor": vendor, "min_ttl_s": min_ttl_s}
    if sub is not None:
        body["sub"] = sub
    if required_scopes is not None:
        body["required_scopes"] = required_scopes
    return requests.post(
        f"{BROKER}/v1/tokens/resolve",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )


def revoke_grant(token: str, vendor: str = "mockhub") -> None:
    """Self-service revoke so a probe starts from a clean needs-consent state."""
    requests.delete(
        f"{BROKER}/v1/grants/{vendor}/{sub_of(token)}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )


def do_consent(token: str, vendor: str = "mockhub") -> None:
    """Run the full §6 dance headlessly: needs-consent -> authorize ->
    mock auto-approves -> broker callback -> connected."""
    r = resolve(token, vendor)
    if r.status_code == 200:
        return
    assert r.status_code == 404, r.text
    page = requests.get(r.json()["authorize_uri"], timeout=15)  # follows 302s
    assert page.status_code == 200 and "Connected" in page.text, page.text
    assert resolve(token, vendor).status_code == 200


def hub_login_as(sub: str | None) -> None:
    """Who the hub stub signs in (None: whoever the login_hint names)."""
    requests.post(f"{HUB}/_test/login_as", json={"sub": sub}, timeout=10).raise_for_status()


def to_vendor(token: str, vendor: str = "mockhub") -> tuple[requests.Session, str]:
    """Walk resolve -> /v1/authorize -> hub login -> /v1/callback/_hub in one
    browser session and return (session, the unfollowed redirect to the
    vendor's authorize endpoint). The session holds the consent binding
    cookie that the vendor callback requires."""
    revoke_grant(token, vendor)
    r = resolve(token, vendor)
    assert r.status_code == 404, f"expected needs-consent, got {r.status_code}: {r.text}"
    browser = requests.Session()
    r = browser.get(r.json()["authorize_uri"], allow_redirects=False, timeout=15)
    assert r.status_code in (302, 307), r.text  # -> hub login
    r = browser.get(r.headers["location"], allow_redirects=False, timeout=15)
    assert r.status_code in (302, 307), r.text  # -> /callback/_hub
    r = browser.get(r.headers["location"], allow_redirects=False, timeout=15)
    assert r.status_code in (302, 307), r.text  # -> vendor
    return browser, r.headers["location"]


def new_consent_state(token: str, vendor: str = "mockhub") -> tuple[requests.Session, str]:
    """(browser session, the unconsumed vendor-callback `state`), so a probe
    can hit /v1/callback directly from the browser that started the flow."""
    browser, location = to_vendor(token, vendor)
    return browser, re.search(r"[?&]state=([^&]+)", location).group(1)


def walk_to_callback(token: str, vendor: str = "mockhub") -> tuple[requests.Session, str]:
    """(browser session, the vendor callback URL), un-redeemed."""
    browser, location = to_vendor(token, vendor)
    r = browser.get(location, allow_redirects=False, timeout=15)  # vendor consents
    assert r.status_code in (302, 307)
    return browser, r.headers["location"]


def mock_state() -> dict:
    return requests.get(f"{MOCK}/_test/state", timeout=10).json()


def mock_reset() -> None:
    requests.post(f"{MOCK}/_test/reset", timeout=10)


@contextmanager
def vendor_token_ttl(seconds: int):
    """Tokens the mock vendor mints meanwhile live `seconds` (normally 60,
    which is inside REFRESH_BUFFER_S, so every resolve refreshes)."""
    requests.post(f"{MOCK}/_test/at_ttl", json={"seconds": seconds}, timeout=10).raise_for_status()
    try:
        yield
    finally:
        requests.post(f"{MOCK}/_test/at_ttl", json={"seconds": 60}, timeout=10).raise_for_status()


def container_audit_events(container: str, since: str = "5m") -> list[dict]:
    """One-line JSON audit records off a container log."""
    out = subprocess.run(
        ["docker", "logs", "--since", since, container], capture_output=True, text=True, check=True
    )
    events = []
    for line in (out.stdout + out.stderr).splitlines():
        for m in re.finditer(r'\{"audit".*?\}', line):
            try:
                events.append(json.loads(m.group(0)))
            except json.JSONDecodeError:
                continue
    return events


def broker_audit(event: str | None = None, since: str = "5m") -> list[dict]:
    """Audit records across every broker replica, oldest-first per replica."""
    events = []
    for container in BROKER_CONTAINERS:
        events.extend(container_audit_events(container, since))
    if event is not None:
        events = [e for e in events if e.get("audit") == event]
    return events


def stack_containers() -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "--filter", "name=vtb-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [n for n in out.stdout.splitlines() if n.strip()]


def grep_container_logs(needle: str, since: str = "30m") -> dict[str, int]:
    hits = {}
    for name in stack_containers():
        out = subprocess.run(
            ["docker", "logs", "--since", since, name], capture_output=True, text=True
        )
        n = (out.stdout + out.stderr).count(needle)
        if n:
            hits[name] = n
    return hits


def wait_for(predicate, timeout: float, interval: float = 2.0, what: str = "condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")
