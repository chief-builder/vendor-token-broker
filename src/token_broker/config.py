"""Broker configuration: fail-fast at startup.

Deployment-specific values (issuer, JWKS, public URL, custody backend,
registry) have no defaults — a missing one aborts startup with every
missing name listed at once. Contract pins keep compatible defaults so an
existing deployment can point at this broker unchanged.
"""
import os
from dataclasses import dataclass, fields
from pathlib import Path


class ConfigError(Exception):
    pass


_REQUIRED = ("HUB_ISSUER", "HUB_JWKS_URI", "BROKER_PUBLIC_URL", "VAULT_ADDR", "REGISTRY_PATH")

# Asymmetric signature algorithms only: RS256 (PKCS#1 v1.5) and every HMAC
# algorithm are outside the hub contract and can never be configured.
ALLOWED_HUB_ALGORITHMS = frozenset(
    {"PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"})

# Knobs where 0 has a meaning (sweeper off / no per-replica cache); every
# other integer knob must be at least 1.
_ZERO_OK = frozenset({"sweep_interval_s", "cache_ttl_s"})


@dataclass(frozen=True)
class Config:
    # Deployment-specific: required, no defaults.
    hub_issuer: str
    hub_jwks_uri: str
    broker_public_url: str
    vault_addr: str
    vault_token: str
    registry_path: Path

    # Contract pins: defaulted-compatible.
    hub_tier_audience: str = "mcp://tier/internal"
    hub_algorithms: tuple[str, ...] = ("PS256", "ES256")
    hub_contract_version: str = "1.0"
    admin_group: str = "mcp-platform-admin"
    problem_urn_prefix: str = "urn:vendor-token-broker"

    # Timing knobs (design §§7-9: cache ≤60s, txn TTL 10 min, lock hard timeout).
    refresh_buffer_s: int = 300
    cache_ttl_s: int = 60
    txn_ttl_s: int = 600
    lock_timeout_s: int = 10
    sweep_interval_s: int = 60
    proactive_refresh_s: int = 900
    mass_stale_window_s: int = 60
    mass_stale_threshold: int = 3
    vault_timeout_s: int = 3

    # Coordination backend: memory (single replica) or redis (multi-replica).
    coord_backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"
    lock_ttl_ms: int = 15000
    refreshing_ttl_s: int = 30

    def __post_init__(self):
        if not self.hub_algorithms:
            raise ConfigError("HUB_ALGORITHMS must name at least one algorithm")
        bad = [a for a in self.hub_algorithms if a not in ALLOWED_HUB_ALGORITHMS]
        if bad:
            raise ConfigError(
                f"HUB_ALGORITHMS may only contain {sorted(ALLOWED_HUB_ALGORITHMS)}, "
                f"not {bad}")
        for f in fields(self):
            if f.type is not int:
                continue
            value = getattr(self, f.name)
            floor = 0 if f.name in _ZERO_OK else 1
            if value < floor:
                raise ConfigError(f"{f.name.upper()} must be >= {floor}, not {value}")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        missing = [name for name in _REQUIRED if not env.get(name)]

        vault_token = env.get("VAULT_TOKEN", "")
        token_file = env.get("VAULT_TOKEN_FILE", "")
        if token_file:
            try:
                vault_token = Path(token_file).read_text().strip()
            except OSError as exc:
                raise ConfigError(f"VAULT_TOKEN_FILE unreadable: {exc}") from exc
        if not vault_token:
            missing.append("VAULT_TOKEN (or VAULT_TOKEN_FILE)")
        if missing:
            raise ConfigError("missing required configuration: " + ", ".join(missing))

        backend = env.get("COORD_BACKEND", "memory")
        if backend not in ("memory", "redis"):
            raise ConfigError(f"COORD_BACKEND must be 'memory' or 'redis', not {backend!r}")

        def _int(name: str, default: int) -> int:
            raw = env.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be an integer, not {raw!r}") from exc

        return cls(
            hub_issuer=env["HUB_ISSUER"],
            hub_jwks_uri=env["HUB_JWKS_URI"],
            broker_public_url=env["BROKER_PUBLIC_URL"].rstrip("/"),
            vault_addr=env["VAULT_ADDR"],
            vault_token=vault_token,
            registry_path=Path(env["REGISTRY_PATH"]),
            hub_tier_audience=env.get("HUB_TIER_AUDIENCE", cls.hub_tier_audience),
            hub_algorithms=tuple(
                a.strip() for a in env.get("HUB_ALGORITHMS", "PS256,ES256").split(",") if a.strip()
            ),
            hub_contract_version=env.get("HUB_CONTRACT_VERSION", cls.hub_contract_version),
            admin_group=env.get("ADMIN_GROUP", cls.admin_group),
            problem_urn_prefix=env.get("PROBLEM_URN_PREFIX", cls.problem_urn_prefix),
            refresh_buffer_s=_int("REFRESH_BUFFER_S", cls.refresh_buffer_s),
            cache_ttl_s=_int("CACHE_TTL_S", cls.cache_ttl_s),
            txn_ttl_s=_int("TXN_TTL_S", cls.txn_ttl_s),
            lock_timeout_s=_int("LOCK_TIMEOUT_S", cls.lock_timeout_s),
            sweep_interval_s=_int("SWEEP_INTERVAL_S", cls.sweep_interval_s),
            proactive_refresh_s=_int("PROACTIVE_REFRESH_S", cls.proactive_refresh_s),
            mass_stale_window_s=_int("MASS_STALE_WINDOW_S", cls.mass_stale_window_s),
            mass_stale_threshold=_int("MASS_STALE_THRESHOLD", cls.mass_stale_threshold),
            vault_timeout_s=_int("VAULT_TIMEOUT_S", cls.vault_timeout_s),
            coord_backend=backend,
            redis_url=env.get("REDIS_URL", cls.redis_url),
            lock_ttl_ms=_int("LOCK_TTL_MS", cls.lock_ttl_ms),
            refreshing_ttl_s=_int("REFRESHING_TTL_S", cls.refreshing_ttl_s),
        )
