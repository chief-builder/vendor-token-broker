#!/bin/sh
# One-shot OpenBao provisioning for the test stack (mirrors the source lab's
# setup): KV v2 mounts, the broker ACL policy, a deterministic *scoped*
# broker token (never root), and the mockhub client credential.
set -eu

BAO="${BAO_ADDR:-http://openbao:8200}"
ROOT="${BAO_ROOT_TOKEN:-root}"
BROKER_TOKEN="${BROKER_VAULT_TOKEN:-vtb-dev-broker-token}"

echo "==> Waiting for OpenBao at ${BAO}..."
i=0
until curl -sf "${BAO}/v1/sys/health" -o /dev/null; do
  i=$((i + 1))
  [ "$i" -ge 60 ] && { echo "OpenBao not ready" >&2; exit 1; }
  sleep 1
done

ensure_mount() {
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Vault-Token: ${ROOT}" \
    -d '{"type":"kv","options":{"version":"2"}}' "${BAO}/v1/sys/mounts/$1")
  case "$code" in 2*|400) ;; *) echo "FAILED to mount $1 ($code)" >&2; exit 1;; esac
}
ensure_mount vendor-tokens
ensure_mount vendor-clients

# Keep only 2 versions per grant entry (enough for check-and-set): KV v2's
# default of 10 would retain superseded token pairs readable by version.
curl -sf -H "X-Vault-Token: ${ROOT}" -d '{"max_versions":2}' \
  "${BAO}/v1/vendor-tokens/config" > /dev/null

# Broker policy: read/write custody, read-only vendor client creds. No human
# read path to token material. Verbatim from the source lab's provisioning.
curl -sf -H "X-Vault-Token: ${ROOT}" -X PUT \
  -d '{"policy":"path \"vendor-tokens/data/*\" { capabilities = [\"create\",\"read\",\"update\",\"delete\"] }\npath \"vendor-tokens/metadata/*\" { capabilities = [\"read\",\"delete\",\"list\"] }\npath \"vendor-clients/data/*\" { capabilities = [\"read\"] }"}' \
  "${BAO}/v1/sys/policies/acl/broker" > /dev/null

# Deterministic scoped token so the broker never runs with root. Re-runs get
# a 400 (id exists) — ignored.
curl -s -o /dev/null -H "X-Vault-Token: ${ROOT}" \
  -d "{\"id\":\"${BROKER_TOKEN}\",\"policies\":[\"broker\"],\"ttl\":\"768h\",\"display_name\":\"broker\",\"no_parent\":true}" \
  "${BAO}/v1/auth/token/create"
# Verify the scoped token actually works before declaring success.
code=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Vault-Token: ${BROKER_TOKEN}" \
  "${BAO}/v1/auth/token/lookup-self")
[ "$code" = "200" ] || { echo "broker token unusable ($code)" >&2; exit 1; }

echo "==> Writing vendor client credentials to vendor-clients/..."
curl -sf -H "X-Vault-Token: ${ROOT}" \
  -d "{\"data\":{\"client_id\":\"${MOCK_CLIENT_ID:-mcp-lab-broker}\",\"client_secret\":\"${MOCK_CLIENT_SECRET:-mock-secret}\"}}" \
  "${BAO}/v1/vendor-clients/data/mockhub" > /dev/null
# Same client, registered as a vendor without a revocation endpoint and as
# the stand-ins for MCP servers with their own sign-in.
for vendor in mockhub-norevoke mockhub-atlassian mockhub-cloudflare; do
  curl -sf -H "X-Vault-Token: ${ROOT}" \
    -d "{\"data\":{\"client_id\":\"${MOCK_CLIENT_ID:-mcp-lab-broker}\",\"client_secret\":\"${MOCK_CLIENT_SECRET:-mock-secret}\"}}" \
    "${BAO}/v1/vendor-clients/data/${vendor}" > /dev/null
done

# private_key_jwt client: the broker-side private key (test-only keypair,
# committed under tests/stack/keys/).
if [ -f /keys/mockhub-jwt-private.pem ]; then
  PK=$(awk 'BEGIN{ORS="\\n"}1' /keys/mockhub-jwt-private.pem)
  curl -sf -H "X-Vault-Token: ${ROOT}" \
    -d "{\"data\":{\"client_id\":\"${MOCK_JWT_CLIENT_ID:-mcp-lab-broker-jwt}\",\"private_key\":\"${PK}\",\"alg\":\"RS256\"}}" \
    "${BAO}/v1/vendor-clients/data/mockhub-jwt" > /dev/null
  echo "==> mockhub-jwt (private_key_jwt) client configured"
fi

if [ -n "${GITHUB_CLIENT_ID:-}" ]; then
  curl -sf -H "X-Vault-Token: ${ROOT}" \
    -d "{\"data\":{\"client_id\":\"${GITHUB_CLIENT_ID}\",\"client_secret\":\"${GITHUB_CLIENT_SECRET}\"}}" \
    "${BAO}/v1/vendor-clients/data/github" > /dev/null
  echo "==> GitHub vendor configured"
fi
if [ -n "${LINEAR_CLIENT_ID:-}" ]; then
  curl -sf -H "X-Vault-Token: ${ROOT}" \
    -d "{\"data\":{\"client_id\":\"${LINEAR_CLIENT_ID}\",\"client_secret\":\"${LINEAR_CLIENT_SECRET}\"}}" \
    "${BAO}/v1/vendor-clients/data/linear" > /dev/null
  echo "==> Linear vendor configured"
fi
# Vendors whose MCP server runs its own sign-in: the broker registered once
# (tools/register-mcp-client.py) and the credentials come from tests/stack/.env.
for v in ATLASSIAN:atlassian CLOUDFLARE:cloudflare; do
  prefix=${v%%:*}; vendor=${v#*:}
  eval "cid=\${${prefix}_CLIENT_ID:-}; csec=\${${prefix}_CLIENT_SECRET:-}"
  if [ -n "$cid" ]; then
    curl -sf -H "X-Vault-Token: ${ROOT}" \
      -d "{\"data\":{\"client_id\":\"${cid}\",\"client_secret\":\"${csec}\"}}" \
      "${BAO}/v1/vendor-clients/data/${vendor}" > /dev/null
    echo "==> ${vendor} vendor configured"
  fi
done

echo "==> OpenBao initialized (scoped broker token ready)"
