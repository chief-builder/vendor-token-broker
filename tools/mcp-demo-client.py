#!/usr/bin/env python3
"""Demo MCP client for the gateway: sign in at the hub with OAuth
(authorization code + PKCE, pre-registered public client), connect GitHub
through the broker when asked, then call a GitHub tool.

Usage (gateway profile running):
    .venv/bin/python tools/mcp-demo-client.py                   # get_me
    .venv/bin/python tools/mcp-demo-client.py list_issues '{"owner": "o", "repo": "r"}'

Two browser windows may open: the hub sign-in (every run) and, the first
time, the GitHub connection. Nothing needs to be pasted back here.
"""
import argparse
import asyncio
import json
import sys
import webbrowser

from fastmcp import Client
from fastmcp.client.auth import OAuth
from fastmcp.client.elicitation import ElicitResult


async def connect_prompt(message, response_type, params, ctx):
    """The gateway asks the user to connect GitHub (URL-mode elicitation).
    Open the link and accept: the gateway waits until the browser flow is
    done, so there is nothing to confirm here."""
    print(f"\n{message}\nOpening {params.url}\n(finish in the browser; waiting...)",
          file=sys.stderr)
    await asyncio.to_thread(webbrowser.open, params.url)
    return ElicitResult(action="accept")


async def main(args) -> int:
    auth = OAuth(mcp_url=args.url, scopes=["openid", "mcp-gateway"],
                 client_id=args.client_id, callback_port=args.callback_port)
    async with Client(args.url, auth=auth, elicitation_handler=connect_prompt,
                      mode=args.mode, timeout=180) as c:
        names = [t.name for t in await c.list_tools()]
        if args.tool not in names:
            r = await c.call_tool("connect_github", {}, raise_on_error=False)
            print(r.content[0].text, file=sys.stderr)
            if r.is_error:
                return 1
            names = [t.name for t in await c.list_tools()]
        print(f"tools: {', '.join(sorted(names))}", file=sys.stderr)
        r = await c.call_tool(args.tool, json.loads(args.arguments), raise_on_error=False)
        for block in r.content:
            print(getattr(block, "text", block))
        return 1 if r.is_error else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("tool", nargs="?", default="get_me")
    p.add_argument("arguments", nargs="?", default="{}", help="tool arguments as JSON")
    p.add_argument("--url", default="http://localhost:8500/mcp")
    p.add_argument("--client-id", default="mcp-demo-cli")
    p.add_argument("--callback-port", type=int, default=33418)
    p.add_argument("--mode", default="legacy", choices=["legacy", "2026-07-28"],
                   help="MCP protocol era to speak to the gateway")
    sys.exit(asyncio.run(main(p.parse_args())))
