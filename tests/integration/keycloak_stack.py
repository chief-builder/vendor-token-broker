"""Helpers for the `gateway` compose profile: Keycloak as the real hub and
vtb-broker-kc trusting it. Drives the same flows a real deployment uses —
the Keycloak login form, authorization code + PKCE for the MCP client, and
RFC 8693 token exchange by the gateway — with no test-only shortcuts.
Uniquely named so bare imports never collide with the unit suite."""
import base64
import hashlib
import html
import json
import re
import secrets
from urllib.parse import parse_qs, urljoin, urlparse

import requests

KC_REALM = "http://localhost:8180/realms/mcp"
KC_AUTH = f"{KC_REALM}/protocol/openid-connect/auth"
KC_TOKEN = f"{KC_REALM}/protocol/openid-connect/token"
BROKER_KC = "http://localhost:8600"
GATEWAY_MCP = "http://localhost:8500/mcp"
MOCK_GITHUB_MCP = "http://localhost:8330"

MCP_CLIENT_ID = "mcp-demo-cli"
MCP_REDIRECT = "http://localhost:33418/callback"
GATEWAY_CLIENT_ID = "mcp-gateway"
GATEWAY_CLIENT_SECRET = "gateway-dev-secret"
GATEWAY_RESOURCE = "http://localhost:8500/mcp"

USERS = {"alice": "a11ce000-0000-4000-8000-000000000001",
         "bob": "b0b00000-0000-4000-8000-000000000002"}

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


def jwt_part(token: str, index: int) -> dict:
    part = token.split(".")[index]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class _Browser(requests.Session):
    """Browsers treat http://*.localhost as a secure context and send Secure
    cookies to it; Keycloak marks its login-session cookies Secure even over
    http. Mirror the browser rule so the test browser behaves like a real one.
    (A cookie policy can't do this: requests copies the jar into a fresh one
    with the default policy when preparing each request.)"""

    def send(self, request, **kwargs):
        response = super().send(request, **kwargs)
        for cookie in self.cookies:
            # http.cookiejar files a bare "localhost" cookie under "localhost.local".
            domain = cookie.domain.lstrip(".").removesuffix(".local")
            if domain == "localhost" or domain.endswith(".localhost"):
                cookie.secure = False
        return response


def browser_session() -> requests.Session:
    return _Browser()


def keycloak_login(browser: requests.Session, login_page: requests.Response,
                   username: str) -> requests.Response:
    """Submit the Keycloak login form shown by `login_page`; return the
    unfollowed redirect back to the relying party."""
    assert login_page.status_code == 200, login_page.text[:300]
    action = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', login_page.text)
    assert action, "Keycloak login form not found"
    return browser.post(html.unescape(action.group(1)),
                        data={"username": username, "password": username, "credentialId": ""},
                        allow_redirects=False, timeout=15)


def register_client(redirect_uri: str = "http://localhost:33419/callback") -> requests.Response:
    """RFC 7591 dynamic registration, as stock MCP clients (Claude Code) do."""
    return requests.post(f"{KC_REALM}/clients-registrations/openid-connect", json={
        "client_name": "dcr-test", "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "scope": "mcp-gateway offline_access"}, timeout=15)


def mcp_token(username: str, scope: str = "openid mcp-gateway",
              client_id: str = MCP_CLIENT_ID, redirect_uri: str = MCP_REDIRECT) -> str:
    """An MCP access token for `username`, obtained the way an MCP client
    does: authorization code + PKCE against Keycloak. Self-registered
    clients get Keycloak's consent screen, which is accepted."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    browser = browser_session()
    page = browser.get(KC_AUTH, params={
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
        "scope": scope, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256"}, timeout=15)
    back = keycloak_login(browser, page, username)
    if "/login-actions/" in back.headers.get("location", ""):       # consent screen
        consent = browser.get(back.headers["location"], timeout=15)
        action = re.search(r'<form[^>]*action="([^"]+)"', consent.text).group(1)
        fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', consent.text))
        back = browser.post(urljoin(consent.url, html.unescape(action)),
                            data={**fields, "accept": "Yes"}, allow_redirects=False, timeout=15)
    assert back.status_code == 302, back.text[:300]
    query = parse_qs(urlparse(back.headers["location"]).query)
    assert query["state"] == [state], query
    r = requests.post(KC_TOKEN, data={
        "grant_type": "authorization_code", "client_id": client_id,
        "code": query["code"][0], "redirect_uri": redirect_uri,
        "code_verifier": verifier}, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def exchange(subject_token: str, client_secret: str = GATEWAY_CLIENT_SECRET
             ) -> requests.Response:
    """The gateway's RFC 8693 exchange: MCP token -> hub JWT for the broker."""
    return requests.post(KC_TOKEN, data={
        "grant_type": TOKEN_EXCHANGE,
        "client_id": GATEWAY_CLIENT_ID, "client_secret": client_secret,
        "subject_token": subject_token, "subject_token_type": ACCESS_TOKEN_TYPE,
        "requested_token_type": ACCESS_TOKEN_TYPE, "scope": "hub-tier"}, timeout=15)


def hub_jwt(username: str) -> str:
    r = exchange(mcp_token(username))
    r.raise_for_status()
    return r.json()["access_token"]


def resolve_kc(token: str, vendor: str = "mockhub", min_ttl_s: int = 30) -> requests.Response:
    return requests.post(f"{BROKER_KC}/v1/tokens/resolve",
                         json={"vendor": vendor, "min_ttl_s": min_ttl_s},
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)


def revoke_kc(token: str, vendor: str = "mockhub") -> None:
    sub = jwt_part(token, 1)["sub"]
    requests.delete(f"{BROKER_KC}/v1/grants/{vendor}/{sub}",
                    headers={"Authorization": f"Bearer {token}"}, timeout=15)


def consent_via_keycloak(authorize_uri: str, username: str) -> requests.Response:
    """Open a broker consent link in a fresh browser, sign in at Keycloak as
    `username`, and follow the rest of the flow; return the final page."""
    browser = browser_session()
    login_page = browser.get(authorize_uri, timeout=15)       # broker -> Keycloak
    back = keycloak_login(browser, login_page, username)       # -> /v1/callback/_hub
    assert back.status_code == 302, back.text[:300]
    return browser.get(back.headers["location"], timeout=15)  # -> vendor -> broker

