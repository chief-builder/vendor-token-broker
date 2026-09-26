"""Fail-fast gateway configuration. Nothing reads the environment at import
time; tests build GatewayConfig directly."""
import os
from dataclasses import dataclass

_REQUIRED = ("GATEWAY_PUBLIC_URL", "HUB_ISSUER", "HUB_JWKS_URI", "HUB_TOKEN_ENDPOINT",
             "GATEWAY_CLIENT_ID", "GATEWAY_CLIENT_SECRET", "BROKER_URL")

DEFAULT_TOOLS = ("get_me", "search_repositories", "get_file_contents", "list_issues",
                 "issue_read", "list_pull_requests", "pull_request_read")


class ConfigError(Exception):
    pass


def _csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _bool(name: str, value: str) -> bool:
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise ConfigError(f"{name} must be true or false, not {value!r}")


@dataclass(frozen=True)
class GatewayConfig:
    public_url: str                  # e.g. http://localhost:8500; the resource is {public_url}/mcp
    hub_issuer: str
    hub_jwks_uri: str
    hub_token_endpoint: str
    client_id: str                   # the gateway's confidential client at the hub
    client_secret: str
    broker_url: str
    hub_algorithm: str = "PS256"
    gateway_scope: str = "mcp-gateway"       # MCP clients must request it (no RFC 8707)
    exchange_scope: str = "hub-tier"         # yields the broker's hub-JWT contract
    vendor: str = "github"
    min_ttl_s: int = 120
    upstream_url: str = "https://api.githubcopilot.com/mcp/"
    upstream_tools: tuple[str, ...] = DEFAULT_TOOLS
    upstream_toolsets: tuple[str, ...] = ("repos", "issues", "pull_requests", "context")
    upstream_readonly: bool = True
    upstream_lockdown: bool = True
    upstream_tool_snapshot: str = ""         # "": bundled GitHub snapshot; "none": no snapshot
    consent_wait_s: int = 120
    http_timeout_s: float = 15.0

    @property
    def resource_url(self) -> str:
        return f"{self.public_url.rstrip('/')}/mcp"

    def __post_init__(self):
        if self.hub_algorithm in ("RS256", "HS256", "HS384", "HS512", "none"):
            raise ConfigError(f"HUB_ALGORITHM {self.hub_algorithm} is not allowed")
        if not self.upstream_tools:
            raise ConfigError("UPSTREAM_TOOLS must name at least one tool")
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
                hub_algorithm=env.get("HUB_ALGORITHM", cls.hub_algorithm),
                gateway_scope=env.get("GATEWAY_SCOPE", cls.gateway_scope),
                exchange_scope=env.get("HUB_EXCHANGE_SCOPE", cls.exchange_scope),
                vendor=env.get("VENDOR", cls.vendor),
                min_ttl_s=int(env.get("MIN_TTL_S", cls.min_ttl_s)),
                upstream_url=env.get("UPSTREAM_MCP_URL", cls.upstream_url),
                upstream_tools=_csv(env["UPSTREAM_TOOLS"]) if "UPSTREAM_TOOLS" in env
                else cls.upstream_tools,
                upstream_toolsets=_csv(env["UPSTREAM_TOOLSETS"]) if "UPSTREAM_TOOLSETS" in env
                else cls.upstream_toolsets,
                upstream_readonly=_bool("UPSTREAM_READONLY", env.get("UPSTREAM_READONLY", "true")),
                upstream_lockdown=_bool("UPSTREAM_LOCKDOWN", env.get("UPSTREAM_LOCKDOWN", "true")),
                upstream_tool_snapshot=env.get("UPSTREAM_TOOL_SNAPSHOT",
                                               cls.upstream_tool_snapshot),
                consent_wait_s=int(env.get("CONSENT_WAIT_S", cls.consent_wait_s)),
                http_timeout_s=float(env.get("HTTP_TIMEOUT_S", cls.http_timeout_s)),
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
