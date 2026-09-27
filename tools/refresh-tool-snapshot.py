#!/usr/bin/env python3
"""Regenerate the pinned tool schemas for one upstream in
src/mcp_gateway/snapshots/: the allowlisted tools exactly as the vendor's MCP
server lists them, fetched with the gateway's own URL, headers, and
Authorization scheme from src/mcp_gateway/upstreams.json.

    UPSTREAM_TOKEN=$(gh auth token) .venv/bin/python tools/refresh-tool-snapshot.py github
    UPSTREAM_TOKEN=<Linear API key> .venv/bin/python tools/refresh-tool-snapshot.py linear

The gateway lists these tools from startup and re-reads the live schemas on
the first connected call, logging any drift from the snapshot.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp_gateway.config import UpstreamSpec
from mcp_gateway.upstream import Upstream

PKG = Path(__file__).resolve().parent.parent / "src" / "mcp_gateway"


async def main(name: str) -> int:
    token = os.environ.get("UPSTREAM_TOKEN")
    if not token:
        print("set UPSTREAM_TOKEN to a token the upstream accepts", file=sys.stderr)
        return 2
    entries = {e["name"]: e for e in json.loads((PKG / "upstreams.json").read_text())["upstreams"]}
    if name not in entries:
        print(f"unknown upstream {name!r}; known: {sorted(entries)}", file=sys.stderr)
        return 2
    e = entries[name]
    spec = UpstreamSpec(name=e["name"], display_name=e["display_name"], vendor=e["vendor"],
                        url=e["url"], tools=tuple(e["tools"]),
                        auth_scheme=e.get("auth_scheme", "Bearer"),
                        headers=dict(e.get("headers", {})), protocol=e.get("protocol", "legacy"))
    listed = {t.name: t for t in await Upstream(spec, 30).list_tools(token)}
    missing = [t for t in spec.tools if t not in listed]
    if missing:
        print(f"{spec.display_name} no longer lists: {missing}; update upstreams.json",
              file=sys.stderr)
        return 1
    tools = [listed[t].model_dump(mode="json", by_alias=True, exclude_none=True,
                                  include={"name", "description", "input_schema", "annotations"})
             for t in spec.tools]
    out = PKG / "snapshots" / e["snapshot"]
    out.write_text(json.dumps({"source": spec.url, "tools": tools}, indent=2) + "\n")
    print(f"wrote {len(tools)} tools to {out.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
