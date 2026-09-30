"""GET /v1/admin/vendors/{vendor}: the group claim must be a list containing
ADMIN_GROUP exactly — never a substring match on a string claim."""

import pytest
from broker_harness import VENDOR, Harness


async def _get(h: Harness, **claims):
    async with h.client() as c:
        return await c.get(
            f"/v1/admin/vendors/{VENDOR}", headers={"Authorization": f"Bearer {h.token(**claims)}"}
        )


async def test_admin_group_in_list_is_allowed():
    h = Harness()
    r = await _get(h, groups=["other", "mcp-platform-admin"])
    assert r.status_code == 200 and r.json()["vendor_id"] == VENDOR


@pytest.mark.parametrize(
    "groups",
    [
        "not-mcp-platform-admin-at-all",  # substring of a string claim
        "mcp-platform-admin",  # even an exact string: must be a list
        ["mcp-platform-admin-readonly"],
        {"mcp-platform-admin": True},
        None,
    ],
)
async def test_non_list_or_non_member_is_forbidden(groups):
    h = Harness()
    r = await _get(h, groups=groups)
    assert r.status_code == 403 and r.json()["title"] == "forbidden"
