# Run locally

Complete one mock-vendor connection, resolve its token, then disconnect it. Requires Docker Compose, Python 3.12, and a checkout of this repository. Run commands from the repository root.

This development stack uses local HTTP, a mock identity provider, test credentials, and in-memory OpenBao storage. Follow [Deploy and operate](operations.md) for production provisioning.

## Start the stack

```sh
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-deps -e .
docker compose -f tests/stack/docker-compose.yml up -d --build --wait
curl -fsS http://localhost:8300/healthz
```

Expected response: `{"ok":true,"custody":"ok"}`. The stack supplies the broker, OpenBao, Redis, a mock vendor, and a hub stub. No real vendor account is needed.

## Connect a mock vendor

The script below plays the **trusted gateway**, so it receives the vendor token internally. It prints only statuses and scope names. A production MCP client must not receive that token.

```sh
.venv/bin/python - <<'PY'
import requests

broker = "http://localhost:8300"
hub = "http://localhost:8320"
subject = "wf-quickstart"
mint = requests.post(f"{hub}/_test/token", json={"sub": subject}, timeout=10)
mint.raise_for_status()
headers = {"Authorization": "Bearer " + mint.json()["access_token"]}
body = {"vendor": "mockhub", "required_scopes": ["issues:read"]}

# Make the walkthrough repeatable for this dedicated test subject.
reset = requests.delete(f"{broker}/v1/grants/mockhub/{subject}",
                        headers=headers, timeout=15)
assert reset.status_code in (200, 404), reset.status_code
challenge = requests.post(f"{broker}/v1/tokens/resolve", json=body,
                          headers=headers, timeout=15)
assert challenge.status_code == 404, challenge.status_code
print("Resolve:", challenge.status_code, challenge.json()["title"])

# Session preserves the consent cookie across hub and vendor redirects.
# The mock hub signs in automatically; a real hub requires browser login.
with requests.Session() as browser:
    connected = browser.get(challenge.json()["authorize_uri"], timeout=15)
    assert connected.status_code == 200 and "Connected" in connected.text
print("Consent: connected")

resolved = requests.post(f"{broker}/v1/tokens/resolve", json=body,
                         headers=headers, timeout=15)
resolved.raise_for_status()
print("Resolve:", resolved.status_code, "scopes:", resolved.json()["granted_scopes"])

deleted = requests.delete(f"{broker}/v1/grants/mockhub/{subject}",
                          headers=headers, timeout=15)
deleted.raise_for_status()
print("Disconnect:", deleted.json())
again = requests.post(f"{broker}/v1/tokens/resolve", json=body,
                      headers=headers, timeout=15)
assert again.status_code == 404
print("Resolve after disconnect:", again.status_code, again.json()["title"])
PY
```

Expected: `404 needs-consent`, successful consent, `200` resolve, `{"revoked": true}`, then `404 needs-consent` again. The mock widens scopes on refresh, so the resolved scope list may include both `issues:read` and `issues:write`; see [scope policy](api.md#scope-policy).

For the browser walkthrough and attack probes, continue to [manual verification](smoke-tests.md).

## Run the automated checks

```sh
.venv/bin/pytest tests/unit -q
.venv/bin/pytest tests/integration -m "not external and not multi and not gateway" -q
```

Switch the standalone broker to Redis to exercise the same contract:

```sh
COORD_BACKEND=redis docker compose -f tests/stack/docker-compose.yml \
  up -d --no-deps --wait broker
.venv/bin/pytest tests/integration -m "not external and not multi and not gateway" -q
```

For multi-replica testing, stop the standalone broker and allow its sweep lease to expire (up to 120 seconds at the default interval) before starting the test. Do not flush a shared Redis instance.

```sh
docker compose -f tests/stack/docker-compose.yml stop broker
docker compose -f tests/stack/docker-compose.yml --profile multi \
  up -d --build --wait broker-a broker-b broker-lb
# Inspect the remaining lease; wait until the previous lease has expired.
docker compose -f tests/stack/docker-compose.yml exec -T redis \
  redis-cli PTTL vtb:sweep-lease
BROKER_URL=http://localhost:8400 BROKER_CONTAINERS=vtb-broker-a,vtb-broker-b \
  .venv/bin/pytest tests/integration/test_multi_replica.py -q
```

The replicas use a 10-second sweep lease. A remaining TTL greater than 10000 ms indicates the previous default-interval lease is still present. The test fixture restarts the standalone broker on completion. Stop the replicas before rerunning the standalone tests so sweepers do not interfere.

## Run the MCP gateway

Start the `gateway` profile: Keycloak as the hub, a broker that trusts it, the [MCP gateway](mcp-gateway.md), and a GitHub stand-in. Keycloak is at `http://localhost:8180` (users `alice`/`alice` and `bob`/`bob`).

```sh
docker compose -f tests/stack/docker-compose.yml --profile gateway up -d --build --wait
.venv/bin/pytest tests/integration -m "gateway and not external" -q
```

### Demo MCP client

```sh
.venv/bin/python tools/mcp-demo-client.py get_me
```

A browser opens on Keycloak: sign in as `alice`. If the gateway asks to connect the vendor, a second tab opens the connection flow; finish it there and the client continues on its own. The client uses the pre-registered public client `mcp-demo-cli` and OAuth callback port 33418.

### Claude Code

```sh
claude mcp add --transport http vtb-gateway http://localhost:8500/mcp --callback-port 33419
```

In a new Claude Code session, run `/mcp`, choose `vtb-gateway`, then **Authenticate**. Claude Code registers itself with Keycloak; sign in as `alice` and approve the consent screen. Then ask it to use a `vtb-gateway` tool such as `get_me`. If GitHub is not connected, Claude Code asks to open a URL; accept, finish in the browser, and the call completes. If authentication later fails with an error naming an old address, run `claude mcp remove vtb-gateway`, add it again, and sign in once.

### Real GitHub

1. Create a GitHub App at <https://github.com/settings/apps/new>: callback URLs `http://localhost:8300/v1/callback/github` and `http://localhost:8600/v1/callback/github`, **Expire user authorization tokens** on, webhook off, repository permissions Contents, Issues, Pull requests, and Metadata **read-only**. Generate a client secret and install the App on the repositories you want to reach.
2. Put `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` in `tests/stack/.env` (gitignored), then run the `up` command above again so custody is provisioned with them.
3. Point the gateway at GitHub's MCP server, with enough time for a person to finish the connection:

   ```sh
   GATEWAY_VENDOR=github GATEWAY_UPSTREAM_URL=https://api.githubcopilot.com/mcp/ \
     GATEWAY_CONSENT_WAIT_S=120 \
     docker compose -f tests/stack/docker-compose.yml --profile gateway up -d --no-deps --wait mcp-gateway
   ```

4. Use the demo client or Claude Code as above; `get_me` returns your GitHub account. `GATEWAY_VENDOR=github .venv/bin/pytest tests/integration/test_external_github_mcp.py -m external` checks the same path.

Recreate the gateway without those variables to return to the stand-in.

## Stop the development stack

```sh
docker compose -f tests/stack/docker-compose.yml --profile multi --profile gateway down
```

OpenBao development storage is lost when its container stops. Run the setup again to reprovision it.
