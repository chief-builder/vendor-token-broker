"""Registry: the example validates against the normative schema, and
enabled_env gating works as documented."""

import json

import jsonschema
import pytest
from unit_helpers import REPO_ROOT

from token_broker.vendors import VendorClient

SCHEMA = json.loads((REPO_ROOT / "schemas" / "vendor-registry.schema.json").read_text())
EXAMPLE = json.loads((REPO_ROOT / "registry.example.json").read_text())


def test_example_registry_validates_against_schema():
    jsonschema.validate(EXAMPLE, SCHEMA)


def test_schema_rejects_metadata_plus_hardcoded_endpoints():
    bad = {
        "dual": {
            **EXAMPLE["mockhub"],
            "vendor_id": "dual",
            "endpoints": {"authorization_endpoint": "https://x/a", "token_endpoint": "https://x/t"},
        }
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, SCHEMA)


def test_schema_rejects_secrets_in_registry():
    bad = {"leaky": {**EXAMPLE["mockhub"], "vendor_id": "leaky", "client_secret": "oops"}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, SCHEMA)


@pytest.fixture
def vendor_client(cfg, custody):
    return VendorClient(cfg, custody)


def test_unknown_vendor_is_none(vendor_client):
    assert vendor_client.get_vendor("nope") is None


def test_enabled_env_gates_vendor(vendor_client, monkeypatch):
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    assert vendor_client.get_vendor("github") is None
    monkeypatch.setenv("GITHUB_CLIENT_ID", "iv1.abc")
    assert vendor_client.get_vendor("github") is not None


def test_ungated_vendor_always_enabled(vendor_client):
    assert vendor_client.get_vendor("mockhub") is not None


async def test_hardcoded_endpoints_used_when_no_metadata(vendor_client):
    eps = await vendor_client.endpoints("github")
    assert eps["token_endpoint"] == "https://github.com/login/oauth/access_token"
