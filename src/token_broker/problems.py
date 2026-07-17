"""RFC 9457 problem responses.

The `title` slugs are wire contract: gateway plugins branch on status codes
and read `title` on plain 409 responses. They must never change. The URN
prefix on `type` is deployment-configurable — no caller reads it.
"""
from fastapi.responses import JSONResponse

# The frozen title vocabulary (wire-compat freeze test asserts this set).
TITLES = frozenset({
    "needs-consent",
    "needs-reconsent-scope",
    "invalid-hub-token",
    "sub-mismatch",
    "unknown-vendor",
    "scope-exceeds-ceiling",
    "revoke-pending",
    "vendor-unavailable",
    "vault-unavailable",
    "coordination-unavailable",
    "forbidden",
    "no-grant",
    "invalid-transaction",
})


class Problems:
    def __init__(self, urn_prefix: str):
        self._prefix = urn_prefix

    def __call__(self, status: int, title: str, detail: str = "", **extra) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={"type": f"{self._prefix}:{title}", "title": title,
                     "detail": detail, **extra},
            media_type="application/problem+json")
