# Broker ACL: read/write custody, read-only vendor client credentials.
# Grants nothing to humans; restricting admin reads is up to your own
# deployment policies. (docs/operations.md)
path "vendor-tokens/data/*" {
  capabilities = ["create", "read", "update", "delete"]
}
path "vendor-tokens/metadata/*" {
  capabilities = ["read", "delete", "list"]
}
path "vendor-clients/data/*" {
  capabilities = ["read"]
}
