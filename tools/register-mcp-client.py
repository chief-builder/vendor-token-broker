#!/usr/bin/env python3
"""Register the broker, once, as an OAuth client of a vendor's MCP
authorization server (RFC 7591 dynamic client registration), for vendors
whose MCP server runs its own sign-in (Atlassian, Cloudflare). Reads the
vendor's registry entry: its `auth_metadata_url` (to find the registration
endpoint) and `scope_ceiling`.

    # local stack: write the credentials into tests/stack/.env
    .venv/bin/python tools/register-mcp-client.py atlassian \\
        --redirect-base http://localhost:8600 --env-file tests/stack/.env

    # a deployment: write them to custody (needs VAULT_ADDR and an admin VAULT_TOKEN)
    .venv/bin/python tools/register-mcp-client.py atlassian \\
        --redirect-base https://broker.example.com --vault

Register ONCE and keep the credentials: registering again creates a new
client, and every connection made with the old one stops working. The
script refuses when credentials already exist unless --force is given. It
never prints the client secret.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CLIENT_NAME = "vtb-mcp-gateway"


def _env_keys(vendor: str) -> tuple[str, str]:
    prefix = re.sub(r"[^A-Z0-9]", "_", vendor.upper())
    return f"{prefix}_CLIENT_ID", f"{prefix}_CLIENT_SECRET"


def _env_has(path: Path, key: str) -> bool:
    return path.exists() and re.search(rf"^{key}=.+$", path.read_text(), re.M) is not None


def _env_set(path: Path, key: str, value: str) -> None:
    text = path.read_text() if path.exists() else ""
    if re.search(rf"^{key}=", text, re.M):
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + f"{key}={value}\n"
    path.write_text(text)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("vendor")
    p.add_argument("--redirect-base", required=True,
                   help="the broker's public URL; the callback is <base>/v1/callback/<vendor>")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--env-file", type=Path, help="write <VENDOR>_CLIENT_ID/_SECRET here")
    where.add_argument("--vault", action="store_true",
                       help="write vendor-clients/<vendor> in custody (VAULT_ADDR, VAULT_TOKEN)")
    p.add_argument("--registry", type=Path,
                   default=Path(os.environ.get("REGISTRY_PATH", ROOT / "registry.example.json")))
    p.add_argument("--force", action="store_true",
                   help="register again even though credentials exist (orphans connections)")
    args = p.parse_args()

    spec = json.loads(args.registry.read_text()).get(args.vendor)
    if not spec or "auth_metadata_url" not in spec:
        print(f"{args.vendor}: needs a registry entry with auth_metadata_url", file=sys.stderr)
        return 2
    id_key, secret_key = _env_keys(args.vendor)

    vault = None
    if args.vault:
        import hvac
        vault = hvac.Client(url=os.environ["VAULT_ADDR"], token=os.environ["VAULT_TOKEN"])
    exists = (_env_has(args.env_file, id_key) if args.env_file else
              _vault_has(vault, args.vendor))
    if exists and not args.force:
        print(f"{args.vendor}: credentials already exist; registering again would disconnect "
              "everyone. Use --force only if you mean that.", file=sys.stderr)
        return 1

    meta = httpx.get(spec["auth_metadata_url"], timeout=30).json()
    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        print(f"{args.vendor}: its authorization server offers no dynamic registration",
              file=sys.stderr)
        return 1
    body = {"client_name": CLIENT_NAME,
            "redirect_uris": [f"{args.redirect_base.rstrip('/')}/v1/callback/{args.vendor}"],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": spec.get("token_endpoint_auth_method",
                                                   "client_secret_post")}
    if spec.get("scope_ceiling"):
        body["scope"] = " ".join(spec["scope_ceiling"])
    r = httpx.post(endpoint, json=body, timeout=30)
    if r.status_code >= 300:
        print(f"{args.vendor}: registration refused: HTTP {r.status_code} {r.text[:200]}",
              file=sys.stderr)
        return 1
    reg = r.json()
    if not reg.get("client_secret"):
        print(f"{args.vendor}: registered without a client secret (public client); the "
              "broker needs a confidential client", file=sys.stderr)
        return 1

    if args.env_file:
        _env_set(args.env_file, id_key, reg["client_id"])
        _env_set(args.env_file, secret_key, reg["client_secret"])
        where_to = f"{args.env_file} ({id_key}, {secret_key})"
    else:
        vault.secrets.kv.v2.create_or_update_secret(
            mount_point="vendor-clients", path=args.vendor,
            secret={"client_id": reg["client_id"], "client_secret": reg["client_secret"]})
        where_to = f"custody vendor-clients/{args.vendor}"
    print(f"{args.vendor}: registered as {CLIENT_NAME!r}; credentials written to {where_to}")
    print(f"  callback: {body['redirect_uris'][0]}  auth: {reg.get('token_endpoint_auth_method')}")
    expires = reg.get("client_secret_expires_at") or 0
    if expires:
        days = int((expires - time.time()) / 86400)
        print(f"  WARNING: the client secret expires on "
              f"{time.strftime('%Y-%m-%d', time.gmtime(expires))} (in {days} days). "
              "Re-register before then; users will need to reconnect.")
    return 0


def _vault_has(vault, vendor: str) -> bool:
    try:
        vault.secrets.kv.v2.read_secret_version(mount_point="vendor-clients", path=vendor,
                                                raise_on_deleted_version=True)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
