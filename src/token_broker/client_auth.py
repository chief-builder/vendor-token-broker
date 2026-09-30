"""Vendor client authentication (design §3): branch on the registry's
token_endpoint_auth_method.

- client_secret_post   — credentials in the form body (the lab's only mode)
- client_secret_basic  — HTTP Basic per RFC 6749 §2.3.1
- private_key_jwt      — RFC 7523 §2.2 client assertion, signed with the
  per-vendor private key from the custody backend's vendor-clients mount
  (credential shape: {client_id, private_key, alg?, kid?}). Preferred where
  the vendor supports it.
"""

import time
import uuid

import jwt

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASSERTION_TTL_S = 300


class ClientAuthError(Exception):
    pass


def token_request_auth(
    method: str,
    creds: dict,
    endpoint_aud: str,
) -> tuple[dict, tuple[str, str] | None]:
    """Return (extra form fields, httpx basic-auth tuple or None) for a
    vendor token/revocation request."""
    client_id = creds["client_id"]
    if method == "client_secret_post":
        if "client_secret" not in creds:
            raise ClientAuthError(f"{client_id}: client_secret_post needs a client_secret")
        return {"client_id": client_id, "client_secret": creds["client_secret"]}, None
    if method == "client_secret_basic":
        if "client_secret" not in creds:
            raise ClientAuthError(f"{client_id}: client_secret_basic needs a client_secret")
        return {}, (client_id, creds["client_secret"])
    if method == "private_key_jwt":
        key = creds.get("private_key")
        if not key:
            raise ClientAuthError(f"{client_id}: private_key_jwt needs a private_key credential")
        now = int(time.time())
        headers = {"kid": creds["kid"]} if creds.get("kid") else None
        assertion = jwt.encode(
            {
                "iss": client_id,
                "sub": client_id,
                "aud": endpoint_aud,
                "jti": str(uuid.uuid4()),
                "exp": now + ASSERTION_TTL_S,
                "iat": now,
            },
            key,
            algorithm=creds.get("alg", "RS256"),
            headers=headers,
        )
        return {
            "client_id": client_id,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": assertion,
        }, None
    raise ClientAuthError(f"unsupported token_endpoint_auth_method {method!r}")
