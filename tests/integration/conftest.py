"""Integration fixtures: drive the tests/stack compose stack — hub-stub
mints the workforce tokens, mockhub is the vendor, OpenBao is custody.

Ported from the source lab's phase5/phase7 conftests, with Keycloak logins
replaced by hub-stub minting.
"""
import base64
import json
import os
import re
import subprocess
import time

import pytest
import requests

# The multi profile points BROKER at the LB and lists both replicas via env.
BROKER = os.environ.get("BROKER_URL", "http://localhost:8300")
MOCK = "http://localhost:8310"
HUB = "http://localhost:8320"
BROKER_CONTAINERS = os.environ.get("BROKER_CONTAINERS", "vtb-broker").split(",")
OPENBAO_CONTAINER = "vtb-openbao"
MOCK_CONTAINER = "vtb-mock-vendor"


@pytest.fixture(scope="session", autouse=True)
def _stack_up():
    """Fail fast with a hint if the compose stack is not running."""
    try:
        requests.get(f"{BROKER}/healthz", timeout=3).raise_for_status()
        requests.get(f"{MOCK}/_test/state", timeout=3).raise_for_status()
        requests.get(f"{HUB}/jwks", timeout=3).raise_for_status()
    except requests.RequestException as exc:
        pytest.exit(f"test stack not reachable ({exc}); start it with:\n"
                    "  docker compose -f tests/stack/docker-compose.yml up -d --build",
                    returncode=3)


def mint(sub: str = "wf-user-1", kind: str = "ps256", **extra) -> str:
    r = requests.post(f"{HUB}/_test/token", json={"sub": sub, "kind": kind, **extra},
                      timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def alice() -> str:
    return mint("wf-alice")


@pytest.fixture(scope="session")
def bob() -> str:
    return mint("wf-bob")


def claims_of(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def sub_of(token: str) -> str:
    return claims_of(token)["sub"]


def resolve(token: str, vendor: str = "mockhub", min_ttl_s: int = 30,
            sub: str | None = None, required_scopes: list[str] | None = None,
            ) -> requests.Response:
    body: dict = {"vendor": vendor, "min_ttl_s": min_ttl_s}
    if sub is not None:
        body["sub"] = sub
    if required_scopes is not None:
        body["required_scopes"] = required_scopes
    return requests.post(f"{BROKER}/v1/tokens/resolve", json=body,
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)


def revoke_grant(token: str, vendor: str = "mockhub") -> None:
    """Self-service revoke so a probe starts from a clean needs-consent state."""
    requests.delete(f"{BROKER}/v1/grants/{vendor}/{sub_of(token)}",
                    headers={"Authorization": f"Bearer {token}"}, timeout=15)


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


def new_consent_state(token: str, vendor: str = "mockhub") -> str:
    """Drive resolve -> needs-consent -> /v1/authorize and capture the
    unconsumed callback `state` from the redirect to the vendor (without
    following it), so a probe can hit /v1/callback directly."""
    revoke_grant(token, vendor)
    r = resolve(token, vendor)
    assert r.status_code == 404, f"expected needs-consent, got {r.status_code}: {r.text}"
    redirect = requests.get(r.json()["authorize_uri"], allow_redirects=False, timeout=15)
    assert redirect.status_code in (302, 307), redirect.text
    return re.search(r"[?&]state=([^&]+)", redirect.headers["Location"]).group(1)


def walk_to_callback(token: str, vendor: str = "mockhub") -> str:
    """Drive the dance manually and return the callback URL un-redeemed."""
    revoke_grant(token, vendor)
    r = resolve(token, vendor)
    assert r.status_code == 404
    r = requests.get(r.json()["authorize_uri"], allow_redirects=False, timeout=10)
    assert r.status_code in (302, 307), r.status_code
    r = requests.get(r.headers["location"], allow_redirects=False, timeout=10)
    assert r.status_code in (302, 307)
    return r.headers["location"]


def mock_state() -> dict:
    return requests.get(f"{MOCK}/_test/state", timeout=10).json()


def mock_reset() -> None:
    requests.post(f"{MOCK}/_test/reset", timeout=10)


def container_audit_events(container: str, since: str = "5m") -> list[dict]:
    """One-line JSON audit records off a container log."""
    out = subprocess.run(["docker", "logs", "--since", since, container],
                         capture_output=True, text=True, check=True)
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
        capture_output=True, text=True, check=True)
    return [n for n in out.stdout.splitlines() if n.strip()]


def grep_container_logs(needle: str, since: str = "30m") -> dict[str, int]:
    hits = {}
    for name in stack_containers():
        out = subprocess.run(["docker", "logs", "--since", since, name],
                             capture_output=True, text=True)
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
