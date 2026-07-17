"""Real-GitHub leg (marker: external): activates only when a GitHub App is
configured on the stack (GITHUB_CLIENT_ID in the compose env + credential
written to vendor-clients/github by openbao-init)."""
import os

import pytest
from stack import resolve

pytestmark = pytest.mark.external


def test_github_leg_when_configured(alice):
    if not os.environ.get("GITHUB_CLIENT_ID"):
        pytest.skip("GitHub App not configured (GITHUB_CLIENT_ID unset)")
    r = resolve(alice, vendor="github", min_ttl_s=120)
    if r.status_code == 404:
        pytest.skip("GitHub consent not yet granted — open once in a browser: "
                    + r.json()["authorize_uri"])
    assert r.status_code == 200
