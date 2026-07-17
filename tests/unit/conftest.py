"""Unit-test fixtures. Importable helpers live in unit_helpers.py (uniquely
named so it never collides with the integration suite's modules when both
directories are collected in one pytest run)."""
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from unit_helpers import MemoryCustody, make_config

from token_broker.config import Config


@pytest.fixture
def cfg() -> Config:
    return make_config()


@pytest.fixture
def custody() -> MemoryCustody:
    return MemoryCustody()


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def ec_key():
    return ec.generate_private_key(ec.SECP256R1())
