"""Fail-fast gateway configuration. Nothing reads the environment at import
time; tests build GatewayConfig directly.

Which MCP servers the gateway fronts comes from an upstreams file (JSON): the
bundled `upstreams.json` unless GATEWAY_UPSTREAMS names another file."""
import json
import os
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import mcp_types

_REQUIRED = ("GATEWAY_PUBLIC_URL", "HUB_ISSUER", "HUB_JWKS_URI", "HUB_TOKEN_ENDPOINT",
             "GATEWAY_CLIENT_ID", "GATEWAY_CLIENT_SECRET", "BROKER_URL")

_NAME = re.compile(r"^[a-z][a-z0-9]{0,19}$")     # becomes the tool-name prefix
AUTH_SCHEMES = ("Bearer", "Sentry-Bearer")       # Authorization scheme the upstream expects
PROTOCOLS = ("legacy", "auto", "2026-07-28")      # fastmcp Client modes


class ConfigError(Exception):
    pass


def _csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


@dataclass(frozen=True)
class UpstreamSpec:
    """One vendor MCP server behind the gateway."""
    name: str                        # tool prefix and connect_<name>; lowercase
    display_name: str                # used in messages ("Connect your Linear account")
    vendor: str                      # the broker's vendor id for this server's tokens
    url: str
    tools: tuple[str, ...]           # allowlist of upstream tool names
    auth_scheme: str = "Bearer"
    headers: dict[str, str] = field(default_factory=dict)
    protocol: str = "legacy"         # GitHub and Linear negotiate at most 2025-11-25
    snapshot: tuple[mcp_types.Tool, ...] = ()   # pinned schemas, listed from startup

    def __post_init__(self):
        if not _NAME.match(self.name):
            raise ConfigError(f"upstream name {self.name!r} must be 1-20 lowercase "
                              "letters or digits, starting with a letter")
        if not self.tools:
            raise ConfigError(f"upstream {self.name}: tools must name at least one tool")
        if self.auth_scheme not in AUTH_SCHEMES:
            raise ConfigError(f"upstream {self.name}: auth_scheme must be one of {AUTH_SCHEMES}")
        if self.protocol not in PROTOCOLS:
            raise ConfigError(f"upstream {self.name}: protocol must be one of {PROTOCOLS}")
        if any(k.lower() == "authorization" for k in self.headers):
            raise ConfigError(f"upstream {self.name}: headers must not set Authorization")


def _read_snapshot(ref: str, base: Path | None) -> tuple[mcp_types.Tool, ...]:
    """A bare file name is a bundled snapshot (mcp_gateway/snapshots/); a
    path is relative to the upstreams file."""
    if not ref:
        return ()
    if "/" in ref and base is not None:
        text = (base / ref).read_text()
    else:
        text = resources.files("mcp_gateway").joinpath("snapshots", ref).read_text()
    return tuple(mcp_types.Tool.model_validate(t) for t in json.loads(text)["tools"])


def load_upstreams(path: str = "", enabled: tuple[str, ...] = ()) -> tuple[UpstreamSpec, ...]:
    """Upstreams from `path` (default: the bundled upstreams.json), keeping
    only `enabled` names when given."""
    try:
        if path:
            text, base = Path(path).read_text(), Path(path).parent
        else:
            text, base = resources.files("mcp_gateway").joinpath("upstreams.json").read_text(), None
        entries = json.loads(text)["upstreams"]
        specs = []
        for e in entries:
            if enabled and e.get("name") not in enabled:
                continue
            specs.append(UpstreamSpec(
                name=e["name"], display_name=e["display_name"], vendor=e["vendor"],
                url=e["url"], tools=tuple(e["tools"]),
                auth_scheme=e.get("auth_scheme", "Bearer"), headers=dict(e.get("headers", {})),
                protocol=e.get("protocol", "legacy"),
                snapshot=_read_snapshot(e.get("snapshot", ""), base)))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"upstreams file {path or '(bundled)'}: {exc}") from exc
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ConfigError(f"upstream names must be unique: {names}")
    unknown = set(enabled) - set(names)
    if unknown:
        raise ConfigError(f"GATEWAY_ENABLED_UPSTREAMS names unknown upstreams: {sorted(unknown)}")
    if not specs:
        raise ConfigError("no upstreams configured")
    return tuple(specs)


@dataclass(frozen=True)
class GatewayConfig:
    public_url: str                  # e.g. http://localhost:8500; the resource is {public_url}/mcp
    hub_issuer: str
    hub_jwks_uri: str
    hub_token_endpoint: str
    client_id: str                   # the gateway's confidential client at the hub
    client_secret: str
    broker_url: str
    upstreams: tuple[UpstreamSpec, ...]
    hub_algorithm: str = "PS256"
    gateway_scope: str = "mcp-gateway"       # MCP clients must request it (no RFC 8707)
    exchange_scope: str = "hub-tier"         # yields the broker's hub-JWT contract
    min_ttl_s: int = 120
    consent_wait_s: int = 120
    http_timeout_s: float = 15.0

    @property
    def resource_url(self) -> str:
        return f"{self.public_url.rstrip('/')}/mcp"

    def __post_init__(self):
        if self.hub_algorithm in ("RS256", "HS256", "HS384", "HS512", "none"):
            raise ConfigError(f"HUB_ALGORITHM {self.hub_algorithm} is not allowed")
        if self.min_ttl_s < 0 or self.consent_wait_s < 0:
            raise ConfigError("MIN_TTL_S and CONSENT_WAIT_S must be >= 0")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "GatewayConfig":
        env = dict(os.environ if env is None else env)
        missing = [k for k in _REQUIRED if not env.get(k)]
        if missing:
            raise ConfigError("missing required configuration: " + ", ".join(missing))
        try:
            return cls(
                public_url=env["GATEWAY_PUBLIC_URL"],
                hub_issuer=env["HUB_ISSUER"],
                hub_jwks_uri=env["HUB_JWKS_URI"],
                hub_token_endpoint=env["HUB_TOKEN_ENDPOINT"],
                client_id=env["GATEWAY_CLIENT_ID"],
                client_secret=env["GATEWAY_CLIENT_SECRET"],
                broker_url=env["BROKER_URL"].rstrip("/"),
                upstreams=load_upstreams(env.get("GATEWAY_UPSTREAMS", ""),
                                         _csv(env.get("GATEWAY_ENABLED_UPSTREAMS", ""))),
                hub_algorithm=env.get("HUB_ALGORITHM", cls.hub_algorithm),
                gateway_scope=env.get("GATEWAY_SCOPE", cls.gateway_scope),
                exchange_scope=env.get("HUB_EXCHANGE_SCOPE", cls.exchange_scope),
                min_ttl_s=int(env.get("MIN_TTL_S", cls.min_ttl_s)),
                consent_wait_s=int(env.get("CONSENT_WAIT_S", cls.consent_wait_s)),
                http_timeout_s=float(env.get("HTTP_TIMEOUT_S", cls.http_timeout_s)),
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
