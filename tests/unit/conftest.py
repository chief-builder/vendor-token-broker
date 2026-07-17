"""Unit-test fixtures: offline Config, an in-memory CAS custody fake, and
locally-minted hub JWTs (no network, no containers)."""
import time
import uuid
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from token_broker.config import Config
from token_broker.custody import CasConflict

REPO_ROOT = Path(__file__).resolve().parents[2]


def make_config(**overrides) -> Config:
    defaults = dict(
        hub_issuer="https://hub.test/realms/mcp-plane",
        hub_jwks_uri="https://hub.test/jwks",
        broker_public_url="http://broker.test:8300",
        vault_addr="http://vault.test:8200",
        vault_token="test-token",
        registry_path=REPO_ROOT / "registry.example.json",
        sweep_interval_s=0,
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture
def cfg() -> Config:
    return make_config()


class MemoryCustody:
    """Dict-backed custody with KV-v2 CAS semantics, for unit tests."""

    def __init__(self):
        self.entries: dict[tuple[str, str], tuple[dict, int]] = {}
        self.clients: dict[str, dict] = {}
        self.fail = False  # set True to simulate backend outage

    def _check(self):
        if self.fail:
            from token_broker.custody import CustodyUnavailable
            raise CustodyUnavailable("simulated outage")

    def read(self, vendor, sub):
        self._check()
        found = self.entries.get((vendor, sub))
        return (dict(found[0]), found[1]) if found else None

    def write(self, vendor, sub, entry, cas=None):
        self._check()
        current = self.entries.get((vendor, sub))
        version = current[1] if current else 0
        if cas is not None and cas != version:
            raise CasConflict(f"check-and-set parameter did not match ({cas} != {version})")
        self.entries[(vendor, sub)] = (dict(entry), version + 1)
        return version + 1

    def delete(self, vendor, sub):
        self._check()
        self.entries.pop((vendor, sub), None)

    def list_subjects(self, vendor):
        self._check()
        return [s for (v, s) in self.entries if v == vendor]

    def read_client(self, vendor):
        self._check()
        return self.clients.get(vendor)


@pytest.fixture
def custody() -> MemoryCustody:
    return MemoryCustody()


# ---------------------------------------------------------- hub JWT minting

class StaticJWKS:
    """Stands in for PyJWKClient: always returns the fixed public key."""

    class _Key:
        def __init__(self, key):
            self.key = key

    def __init__(self, public_key):
        self._public = public_key

    def get_signing_key_from_jwt(self, token):
        return self._Key(self._public)


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def mint_hub_token(key, alg: str, cfg: Config, **claim_overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": cfg.hub_issuer,
        "sub": "wf-user-1",
        "aud": [cfg.hub_tier_audience],
        "exp": now + 300,
        "iat": now,
        "jti": str(uuid.uuid4()),
        "mcp_contract": cfg.hub_contract_version,
    }
    for k, v in claim_overrides.items():
        if v is None:
            claims.pop(k, None)
        else:
            claims[k] = v
    return jwt.encode(claims, key, algorithm=alg)
