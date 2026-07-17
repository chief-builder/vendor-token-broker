# vendor-token-broker

OAuth credential custodian for third-party SaaS vendors: acquires, custodies,
refreshes, and revokes vendor tokens per enterprise user, and resolves them
per-request for an egress gateway. **Custodian, not issuer** — the broker
holds no signing keys and exposes no token-minting or JWKS endpoint.

Full quickstart and documentation land with v1.0.0 (see `docs/`).
