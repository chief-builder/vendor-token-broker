# Broker ACL: read/write custody, read-only vendor client credentials.
# No human read path to token material. (docs/operations.md)
path "vendor-tokens/data/*" {
  capabilities = ["create", "read", "update", "delete"]
}
path "vendor-tokens/metadata/*" {
  capabilities = ["read", "delete", "list"]
}
path "vendor-clients/data/*" {
  capabilities = ["read"]
}
