"""Problem responses: RFC 9457 shape and the frozen title vocabulary."""

import json

from token_broker.problems import TITLES, Problems


def _body(resp) -> dict:
    return json.loads(resp.body)


def test_problem_shape_and_media_type():
    p = Problems("urn:vendor-token-broker")
    resp = p(409, "revoke-pending", "entry is being revoked")
    assert resp.status_code == 409
    assert resp.media_type == "application/problem+json"
    body = _body(resp)
    assert body["type"] == "urn:vendor-token-broker:revoke-pending"
    assert body["title"] == "revoke-pending"
    assert body["detail"] == "entry is being revoked"


def test_extra_fields_pass_through():
    p = Problems("urn:x")
    body = _body(p(404, "needs-consent", authorize_uri="http://b/v1/authorize/v?txn=t"))
    assert body["authorize_uri"] == "http://b/v1/authorize/v?txn=t"


def test_configurable_prefix_never_touches_title():
    body = _body(Problems("urn:acme:broker")(503, "vault-unavailable"))
    assert body["type"] == "urn:acme:broker:vault-unavailable"
    assert body["title"] == "vault-unavailable"


def test_frozen_titles_present():
    # The wire contract set (handler.lua reads `title` on plain 409s).
    for title in (
        "needs-consent",
        "needs-reconsent-scope",
        "revoke-pending",
        "vendor-unavailable",
        "vault-unavailable",
    ):
        assert title in TITLES
