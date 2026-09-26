#!/usr/bin/env python3
"""Regenerate src/mcp_gateway/github_tools.json: the schemas of the gateway's
default allowlisted tools, as GitHub's MCP server lists them with the
gateway's own headers (read-only, lockdown, toolsets).

    GITHUB_TOKEN=$(gh auth token) .venv/bin/python tools/refresh-github-tool-snapshot.py

The gateway lists these tools from startup and re-reads the live schemas on
the first connected call, logging any drift from this snapshot.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp_gateway.config import DEFAULT_TOOLS, GatewayConfig
from mcp_gateway.upstream import Upstream

OUT = Path(__file__).resolve().parent.parent / "src" / "mcp_gateway" / "github_tools.json"


async def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("set GITHUB_TOKEN (e.g. $(gh auth token))", file=sys.stderr)
        return 2
    cfg = GatewayConfig(public_url="http://unused", hub_issuer="http://unused",
                        hub_jwks_uri="http://unused", hub_token_endpoint="http://unused",
                        client_id="unused", client_secret="unused", broker_url="http://unused")
    listed = {t.name: t for t in await Upstream(cfg).list_tools(token)}
    missing = [name for name in DEFAULT_TOOLS if name not in listed]
    if missing:
        print(f"GitHub no longer lists: {missing}; update DEFAULT_TOOLS", file=sys.stderr)
        return 1
    tools = [listed[name].model_dump(mode="json", by_alias=True, exclude_none=True,
                                     include={"name", "description", "input_schema",
                                              "annotations"})
             for name in DEFAULT_TOOLS]
    OUT.write_text(json.dumps({"source": cfg.upstream_url, "tools": tools}, indent=2) + "\n")
    print(f"wrote {len(tools)} tools to {OUT.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
