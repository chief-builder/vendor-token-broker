"""Scope math (design §4.1/§4.2): consent requests the minimum; re-consent
unions held + required, always capped by the registry ceiling."""

from token_broker.main import consent_scopes, reconsent_scopes

CEILING = ["issues:read", "issues:write", "repo:status"]


def test_no_required_scopes_requests_full_ceiling():
    assert consent_scopes([], CEILING) == CEILING


def test_required_scopes_capped_by_ceiling():
    assert consent_scopes(["issues:read", "admin:org"], CEILING) == ["issues:read"]


def test_required_subset_requests_only_that_subset():
    assert consent_scopes(["issues:write"], CEILING) == ["issues:write"]


def test_reconsent_unions_held_and_required():
    got = reconsent_scopes(held=["issues:read"], required=["issues:write"], ceiling=CEILING)
    assert got == ["issues:read", "issues:write"]


def test_reconsent_never_exceeds_ceiling():
    got = reconsent_scopes(
        held=["issues:read", "legacy:scope"], required=["admin:org"], ceiling=CEILING
    )
    assert got == ["issues:read"]


def test_reconsent_preserves_ceiling_order():
    got = reconsent_scopes(held=["repo:status"], required=["issues:read"], ceiling=CEILING)
    assert got == ["issues:read", "repo:status"]
