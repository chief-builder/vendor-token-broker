"""Grant lifecycle (design §4.4/§4.5): DELETE revokes at the vendor first
(RFC 7009), grants are self-service, and /v1/grants lists only the caller's
connections. Ported from the lab's phase5 gate 4."""
import requests
from conftest import BROKER, broker_audit, do_consent, mint, mock_state, resolve, sub_of


def test_delete_grant_revokes_at_vendor(alice):
    do_consent(alice)
    revokes_before = mock_state()["counters"]["revoke"]
    sub = sub_of(alice)

    r = requests.delete(f"{BROKER}/v1/grants/mockhub/{sub}",
                        headers={"Authorization": f"Bearer {alice}"}, timeout=10)
    assert r.status_code == 200 and r.json()["revoked"] is True

    assert mock_state()["counters"]["revoke"] == revokes_before + 1  # RFC 7009 hit
    assert resolve(alice).status_code == 404                          # entry gone
    grants = requests.get(f"{BROKER}/v1/grants",
                          headers={"Authorization": f"Bearer {alice}"}, timeout=10)
    assert all(g["vendor"] != "mockhub" for g in grants.json()["grants"])
    audit = broker_audit("broker.revoke")
    assert audit and audit[-1]["outcome"] == "revoked"


def test_grants_are_self_service_only(alice, bob):
    r = requests.delete(f"{BROKER}/v1/grants/mockhub/{sub_of(alice)}",
                        headers={"Authorization": f"Bearer {bob}"}, timeout=10)
    assert r.status_code == 403


def test_delete_without_grant_is_404(alice):
    requests.delete(f"{BROKER}/v1/grants/mockhub/{sub_of(alice)}",
                    headers={"Authorization": f"Bearer {alice}"}, timeout=10)
    r = requests.delete(f"{BROKER}/v1/grants/mockhub/{sub_of(alice)}",
                        headers={"Authorization": f"Bearer {alice}"}, timeout=10)
    assert r.status_code == 404
    assert r.json()["title"] == "no-grant"


def test_grants_listing_shows_own_connection(alice):
    do_consent(alice)
    r = requests.get(f"{BROKER}/v1/grants",
                     headers={"Authorization": f"Bearer {alice}"}, timeout=10)
    assert r.status_code == 200
    mock = [g for g in r.json()["grants"] if g["vendor"] == "mockhub"]
    assert mock and mock[0]["state"] == "ACTIVE"
    assert mock[0]["vendor_user_id"] == "mock-4217"


def test_admin_vendor_record_requires_group(alice):
    r = requests.get(f"{BROKER}/v1/admin/vendors/mockhub",
                     headers={"Authorization": f"Bearer {alice}"}, timeout=10)
    assert r.status_code == 403
    denies = broker_audit("broker.admin.deny")
    assert denies and denies[-1]["reason"] == "not_platform_admin"


def test_admin_vendor_record_with_group():
    admin = mint("wf-admin", groups=["mcp-platform-admin"])
    r = requests.get(f"{BROKER}/v1/admin/vendors/mockhub",
                     headers={"Authorization": f"Bearer {admin}"}, timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body["vendor_id"] == "mockhub"
    assert not any("secret" in k for k in body)
