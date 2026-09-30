"""Integration fixtures. Importable helpers live in stack.py (uniquely named
so it never collides with the unit suite's modules when both directories are
collected in one pytest run)."""

import time

import pytest
import requests
from stack import BROKER, HUB, MOCK, mint


@pytest.fixture(scope="session", autouse=True)
def _stack_up():
    """Fail with a hint if the compose stack is not running. Retries briefly:
    a broker container may still be (re)starting from a previous run."""
    deadline = time.time() + 30
    while True:
        try:
            requests.get(f"{BROKER}/healthz", timeout=3).raise_for_status()
            requests.get(f"{MOCK}/_test/state", timeout=3).raise_for_status()
            requests.get(f"{HUB}/jwks", timeout=3).raise_for_status()
            return
        except requests.RequestException as exc:
            if time.time() >= deadline:
                pytest.exit(
                    f"test stack not reachable ({exc}); start it with:\n"
                    "  docker compose -f tests/stack/docker-compose.yml up -d --build",
                    returncode=3,
                )
            time.sleep(2)


@pytest.fixture(scope="session")
def alice() -> str:
    return mint("wf-alice")


@pytest.fixture(scope="session")
def bob() -> str:
    return mint("wf-bob")
