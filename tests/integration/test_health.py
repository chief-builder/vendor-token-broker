"""Startup verification and /healthz against the deployed stack (review H4):
a custody outage is reported but stays healthy (cache hits keep serving),
and a broker with a rejected custody token refuses to start at all."""

import subprocess
from pathlib import Path

import requests
from stack import BROKER, OPENBAO_CONTAINER, wait_for

COMPOSE = Path(__file__).resolve().parents[1] / "stack" / "docker-compose.yml"


def health() -> requests.Response:
    return requests.get(f"{BROKER}/healthz", timeout=10)


def test_healthz_reports_custody_ok():
    r = health()
    assert r.status_code == 200
    assert r.json() == {"ok": True, "custody": "ok"}


def test_custody_outage_is_reported_but_stays_healthy():
    subprocess.run(["docker", "pause", OPENBAO_CONTAINER], check=True, capture_output=True)
    try:
        # The result is cached for 10s; the outage shows once the cache expires.
        r = wait_for(
            lambda: (lambda r: r if r.json()["custody"] == "unreachable" else None)(health()),
            timeout=30,
            interval=1,
            what="healthz to report the outage",
        )
        assert r.status_code == 200 and r.json()["ok"] is True
    finally:
        subprocess.run(["docker", "unpause", OPENBAO_CONTAINER], check=True, capture_output=True)
    wait_for(
        lambda: health().json()["custody"] == "ok",
        timeout=30,
        interval=1,
        what="healthz to recover after unpause",
    )


def test_broker_with_rejected_custody_token_refuses_to_start():
    out = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "VAULT_TOKEN=not-a-real-token",
            "broker",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    logs = out.stdout + out.stderr
    assert out.returncode != 0, logs[-2000:]
    assert "custody token rejected" in logs, logs[-2000:]
    assert "not-a-real-token" not in logs  # the token itself is never echoed
