"""Config fail-fast: every missing required name is listed at once; lab
defaults are gone; contract pins stay defaulted-compatible."""
import pytest

from token_broker.config import Config, ConfigError

FULL_ENV = {
    "HUB_ISSUER": "https://hub.test/realms/mcp-plane",
    "HUB_JWKS_URI": "https://hub.test/jwks",
    "BROKER_PUBLIC_URL": "http://broker.test:8300",
    "VAULT_ADDR": "http://vault.test:8200",
    "VAULT_TOKEN": "tok",
    "REGISTRY_PATH": "/app/registry.json",
    "HUB_LOGIN_CLIENT_ID": "vtb-broker",
}


def test_empty_env_lists_every_missing_name():
    with pytest.raises(ConfigError) as exc:
        Config.from_env({})
    msg = str(exc.value)
    for name in ("HUB_ISSUER", "HUB_JWKS_URI", "BROKER_PUBLIC_URL",
                 "VAULT_ADDR", "REGISTRY_PATH", "VAULT_TOKEN", "HUB_LOGIN_CLIENT_ID"):
        assert name in msg


def test_full_env_loads():
    cfg = Config.from_env(FULL_ENV)
    assert cfg.hub_issuer == FULL_ENV["HUB_ISSUER"]
    assert str(cfg.registry_path) == "/app/registry.json"


def test_contract_pins_default_compatible():
    cfg = Config.from_env(FULL_ENV)
    assert cfg.hub_tier_audience == "mcp://tier/internal"
    assert cfg.hub_algorithms == ("PS256", "ES256")
    assert cfg.hub_contract_version == "1.0"
    assert cfg.admin_group == "mcp-platform-admin"
    assert cfg.problem_urn_prefix == "urn:vendor-token-broker"
    assert cfg.coord_backend == "memory"


def test_vault_token_file_wins(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n")
    env = {**FULL_ENV, "VAULT_TOKEN": "", "VAULT_TOKEN_FILE": str(token_file)}
    assert Config.from_env(env).vault_token == "file-token"


def test_missing_vault_token_file_is_a_config_error(tmp_path):
    env = {**FULL_ENV, "VAULT_TOKEN_FILE": str(tmp_path / "absent")}
    with pytest.raises(ConfigError):
        Config.from_env(env)


def test_bad_coord_backend_rejected():
    with pytest.raises(ConfigError):
        Config.from_env({**FULL_ENV, "COORD_BACKEND": "zookeeper"})


def test_bad_int_rejected():
    with pytest.raises(ConfigError):
        Config.from_env({**FULL_ENV, "REFRESH_BUFFER_S": "soon"})


def test_public_url_trailing_slash_stripped():
    cfg = Config.from_env({**FULL_ENV, "BROKER_PUBLIC_URL": "http://b.test/"})
    assert cfg.broker_public_url == "http://b.test"


@pytest.mark.parametrize("algs", ["RS256", "HS256", "PS256,RS256", "none", " , "])
def test_hub_algorithms_outside_the_allowlist_rejected(algs):
    with pytest.raises(ConfigError):
        Config.from_env({**FULL_ENV, "HUB_ALGORITHMS": algs})


def test_hub_algorithms_allowlist_accepts_asymmetric():
    cfg = Config.from_env({**FULL_ENV, "HUB_ALGORITHMS": "PS256,ES384,EdDSA"})
    assert cfg.hub_algorithms == ("PS256", "ES384", "EdDSA")


@pytest.mark.parametrize("name", ["REFRESH_BUFFER_S", "LOCK_TIMEOUT_S", "LOCK_TTL_MS",
                                  "MASS_STALE_THRESHOLD", "VAULT_TIMEOUT_S", "TXN_TTL_S"])
def test_non_positive_timing_knobs_rejected(name):
    for bad in ("0", "-5"):
        with pytest.raises(ConfigError):
            Config.from_env({**FULL_ENV, name: bad})


def test_zero_allowed_where_it_means_off():
    cfg = Config.from_env({**FULL_ENV, "SWEEP_INTERVAL_S": "0", "CACHE_TTL_S": "0"})
    assert cfg.sweep_interval_s == 0 and cfg.cache_ttl_s == 0
    with pytest.raises(ConfigError):
        Config.from_env({**FULL_ENV, "SWEEP_INTERVAL_S": "-1"})


def test_direct_construction_is_validated_too():
    from unit_helpers import make_config
    with pytest.raises(ConfigError):
        make_config(hub_algorithms=("RS256",))


def test_sweep_max_entries_knob():
    assert Config.from_env(FULL_ENV).sweep_max_entries == 500
    assert Config.from_env({**FULL_ENV, "SWEEP_MAX_ENTRIES": "50"}).sweep_max_entries == 50
    with pytest.raises(ConfigError):
        Config.from_env({**FULL_ENV, "SWEEP_MAX_ENTRIES": "0"})
