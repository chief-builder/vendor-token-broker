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


_REQUIRED = (
    "HUB_ISSUER",
    "HUB_JWKS_URI",
    "BROKER_PUBLIC_URL",
    "VAULT_ADDR",
    "REGISTRY_PATH",
    "HUB_LOGIN_CLIENT_ID",
)

# Asymmetric signature algorithms only: RS256 (PKCS#1 v1.5) and every HMAC
# algorithm are outside the hub contract and can never be configured.
ALLOWED_HUB_ALGORITHMS = frozenset({"PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"})

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
    # The broker's OIDC client at the hub, for the consent-leg login (H1).
    hub_login_client_id: str

    # Contract pins: defaulted-compatible.
    hub_tier_audience: str = "mcp://tier/internal"
    hub_algorithms: tuple[str, ...] = ("PS256", "ES256")
    hub_contract_version: str = "1.0"
    admin_group: str = "mcp-platform-admin"
    hub_login_client_secret: str = ""  # empty: public client (PKCE only)
    # OIDC login_hint on the consent-leg login: "sub" suits hubs that key
    # sign-in on the subject (hub-stub); real IdPs expect a username or email,
    # so send "none". UX only: the logged-in sub is still checked (H1).
    hub_login_hint: str = "sub"
    # Where to fetch the hub's OIDC discovery document. Empty: derived from
    # HUB_ISSUER. Set it when the broker reaches the hub by an internal URL
    # that differs from the public issuer; the document must still name
    # HUB_ISSUER as its issuer.
    hub_discovery_url: str = ""
    problem_urn_prefix: str = "urn:vendor-token-broker"

    # Timing knobs (design §§7-9: cache ≤60s, txn TTL 10 min, lock hard timeout).
    refresh_buffer_s: int = 300
    cache_ttl_s: int = 60
    txn_ttl_s: int = 600
    lock_timeout_s: int = 10
    sweep_interval_s: int = 60
    sweep_max_entries: int = 500
    proactive_refresh_s: int = 900
    mass_stale_window_s: int = 60
    mass_stale_threshold: int = 3
    vault_timeout_s: int = 3
    vendor_timeout_s: int = 10
    jwks_timeout_s: int = 5
    startup_timeout_s: int = 30

    # Coordination backend: memory (single replica) or redis (multi-replica).
    coord_backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"
    lock_ttl_ms: int = 20000
    refreshing_ttl_s: int = 30

    def __post_init__(self):
        if self.hub_login_hint not in ("sub", "none"):
            raise ConfigError(
                f"HUB_LOGIN_HINT must be 'sub' or 'none', not {self.hub_login_hint!r}"
            )
        if not self.hub_algorithms:
            raise ConfigError("HUB_ALGORITHMS must name at least one algorithm")
        bad = [a for a in self.hub_algorithms if a not in ALLOWED_HUB_ALGORITHMS]
        if bad:
            raise ConfigError(
                f"HUB_ALGORITHMS may only contain {sorted(ALLOWED_HUB_ALGORITHMS)}, not {bad}"
            )
        for f in fields(self):
            if f.type is not int:
                continue
            value = getattr(self, f.name)
            floor = 0 if f.name in _ZERO_OK else 1
            if value < floor:
                raise ConfigError(f"{f.name.upper()} must be >= {floor}, not {value}")
        # The redis lock must outlive the slowest refresh it protects: one
        # vendor round-trip plus the REFRESHING-marker and outcome custody
        # writes. Otherwise a second replica can take the lock mid-refresh
        # (the CAS still keeps custody correct, but the RT family may burn).
        worst_ms = (self.vendor_timeout_s + 2 * self.vault_timeout_s) * 1000
        if self.coord_backend == "redis" and self.lock_ttl_ms < worst_ms:
            raise ConfigError(
                f"LOCK_TTL_MS ({self.lock_ttl_ms}) must be >= (VENDOR_TIMEOUT_S + "
                f"2 x VAULT_TIMEOUT_S) x 1000 = {worst_ms}"
            )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
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
            hub_login_client_id=env["HUB_LOGIN_CLIENT_ID"],
            hub_login_client_secret=env.get("HUB_LOGIN_CLIENT_SECRET", ""),
            hub_login_hint=env.get("HUB_LOGIN_HINT", cls.hub_login_hint),
            hub_discovery_url=env.get("HUB_DISCOVERY_URL", cls.hub_discovery_url),
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
            sweep_max_entries=_int("SWEEP_MAX_ENTRIES", cls.sweep_max_entries),
            proactive_refresh_s=_int("PROACTIVE_REFRESH_S", cls.proactive_refresh_s),
            mass_stale_window_s=_int("MASS_STALE_WINDOW_S", cls.mass_stale_window_s),
            mass_stale_threshold=_int("MASS_STALE_THRESHOLD", cls.mass_stale_threshold),
            vault_timeout_s=_int("VAULT_TIMEOUT_S", cls.vault_timeout_s),
            vendor_timeout_s=_int("VENDOR_TIMEOUT_S", cls.vendor_timeout_s),
            jwks_timeout_s=_int("JWKS_TIMEOUT_S", cls.jwks_timeout_s),
            startup_timeout_s=_int("STARTUP_TIMEOUT_S", cls.startup_timeout_s),
            coord_backend=backend,
            redis_url=env.get("REDIS_URL", cls.redis_url),
            lock_ttl_ms=_int("LOCK_TTL_MS", cls.lock_ttl_ms),
            refreshing_ttl_s=_int("REFRESHING_TTL_S", cls.refreshing_ttl_s),
        )
