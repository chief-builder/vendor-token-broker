"""client_auth: the three token_endpoint_auth_method branches (design §3)."""

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization

from token_broker.client_auth import ASSERTION_TYPE, ClientAuthError, token_request_auth

SECRET_CREDS = {"client_id": "cid", "client_secret": "shh"}


def test_client_secret_post_puts_credentials_in_form():
    form, basic = token_request_auth("client_secret_post", SECRET_CREDS, "https://as/token")
    assert form == {"client_id": "cid", "client_secret": "shh"}
    assert basic is None


def test_client_secret_basic_uses_http_basic_only():
    form, basic = token_request_auth("client_secret_basic", SECRET_CREDS, "https://as/token")
    assert form == {}  # RFC 6749 §2.3: never two auth mechanisms at once
    assert basic == ("cid", "shh")


def test_private_key_jwt_builds_a_valid_rfc7523_assertion(rsa_key):
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    creds = {"client_id": "jwt-cid", "private_key": pem, "kid": "k1"}
    form, basic = token_request_auth("private_key_jwt", creds, "https://as/token")
    assert basic is None
    assert form["client_id"] == "jwt-cid"
    assert form["client_assertion_type"] == ASSERTION_TYPE
    claims = pyjwt.decode(
        form["client_assertion"],
        rsa_key.public_key(),
        algorithms=["RS256"],
        audience="https://as/token",
        options={"require": ["exp", "iat", "jti"]},
    )
    assert claims["iss"] == claims["sub"] == "jwt-cid"
    assert pyjwt.get_unverified_header(form["client_assertion"])["kid"] == "k1"


def test_private_key_jwt_without_key_is_an_error():
    with pytest.raises(ClientAuthError):
        token_request_auth("private_key_jwt", {"client_id": "x"}, "https://as/token")


def test_secret_methods_without_secret_are_errors():
    for method in ("client_secret_post", "client_secret_basic"):
        with pytest.raises(ClientAuthError):
            token_request_auth(method, {"client_id": "x"}, "https://as/token")


def test_unknown_method_is_an_error():
    with pytest.raises(ClientAuthError):
        token_request_auth("tls_client_auth", SECRET_CREDS, "https://as/token")
