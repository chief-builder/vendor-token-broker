"""tools/register-mcp-client.py against mock-vendor's MCP authorization
server: one dynamic registration with the broker's callback and scope
ceiling, credentials written where asked, the secret never printed, and a
second run refused (registering again would disconnect everyone)."""

import json
import subprocess
import sys
from pathlib import Path

from stack import mock_state

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "register-mcp-client.py"


def _register(tmp_path: Path) -> subprocess.CompletedProcess:
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "mockhub-atlassian": {
                    "vendor_id": "mockhub-atlassian",
                    "auth_metadata_url": "http://localhost:8310/.well-known/oauth-authorization-server/mcp",
                    "token_endpoint_auth_method": "client_secret_post",
                    "scope_ceiling": ["issues:read"],
                }
            }
        )
    )
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "mockhub-atlassian",
            "--redirect-base",
            "http://localhost:8600",
            "--env-file",
            str(tmp_path / ".env"),
            "--registry",
            str(registry),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_registers_once_and_never_prints_the_secret(tmp_path):
    r = _register(tmp_path)
    assert r.returncode == 0, r.stderr
    written = (tmp_path / ".env").read_text()
    env = dict(line.split("=", 1) for line in written.splitlines())
    client_id, secret = env["MOCKHUB_ATLASSIAN_CLIENT_ID"], env["MOCKHUB_ATLASSIAN_CLIENT_SECRET"]
    assert secret and secret not in r.stdout + r.stderr

    [reg] = [c for c in mock_state()["registered"] if c["client_id"] == client_id]
    assert reg["client_name"] == "vtb-mcp-gateway"
    assert reg["redirect_uris"] == ["http://localhost:8600/v1/callback/mockhub-atlassian"]
    assert reg["scope"] == "issues:read"

    again = _register(tmp_path)
    assert again.returncode == 1 and "already exist" in again.stderr
    assert (tmp_path / ".env").read_text() == written
