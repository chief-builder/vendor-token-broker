"""The consent dance (design §6, review H1): GET /v1/authorize/{vendor}
binds the flow to the browser and sends it to sign in at the hub; the hub
returns to /v1/callback/_hub, which checks the signed-in user and starts the
vendor leg; the vendor returns to /v1/callback/{vendor}, which redeems the
code and stores the grant. Every leg requires the same browser (a binding
cookie), and every state is single use."""

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import refresh as refresh_mod
from . import vendors as vendors_mod
from .audit import audit
from .broker import Broker
from .coordination import CoordinationUnavailable
from .custody import CustodyUnavailable
from .hub import HubUnavailable
from .hub_login import HubLoginError
from .refresh import entry_from_token_response

# Browser-facing responses carry the vendor code/state in the URL: never
# cache them, never leak the URL as a Referer, never sniff the content type.
BROWSER_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def page(text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"<h1>{text}</h1>", status_code=status, headers=BROWSER_HEADERS)


# Consent-leg login (review H1). The hub redirects back to this pseudo-vendor
# on the existing callback route; "_" can never appear in a vendor id.
HUB_LEG = "_hub"
AUTHORIZE_LINK_TTL_S = 300  # an authorize link starts one flow, within 5 min


def binding_cookie(vendor: str) -> str:
    return f"vtb_consent_{vendor}"


def binding_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def hub_redirect(b: Broker) -> str:
    return f"{b.cfg.broker_public_url}/v1/callback/{HUB_LEG}"


def bound_to_this_browser(request: Request, record: dict) -> bool:
    """The flow's binding cookie is present in this browser. Every consent
    leg requires it, so no leg can be completed in another browser (a
    forwarded link or callback is useless to whoever receives it)."""
    value = request.cookies.get(binding_cookie(record["vendor"]), "")
    return bool(value) and hmac.compare_digest(binding_hash(value), record.get("binding", ""))


async def authorize(b: Broker, vendor: str, txn: str) -> Response:
    """Start consent: bind the flow to this browser, then send it to log in
    at the hub; the vendor leg starts only for the user the link was
    issued to (review H1, design §6)."""
    cfg, problem = b.cfg, b.problem
    try:
        record = await b.coord.take_txn(txn)  # one link starts one flow
    except CoordinationUnavailable as exc:
        return problem(503, "coordination-unavailable", str(exc))
    if (
        record is None
        or record["vendor"] != vendor
        or time.time() - record["created_at"] > AUTHORIZE_LINK_TTL_S
    ):
        audit("broker.consent.fail", vendor=vendor, reason="bad_txn", security_event=False)
        return problem(400, "invalid-transaction", "unknown or expired transaction")
    if b.vendors.get_vendor(vendor) is None:
        return problem(404, "unknown-vendor", vendor)

    binding = secrets.token_urlsafe(32)
    hub_state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    verifier, challenge = pkce_pair()
    try:
        login_url = await b.hub_login.authorization_url(
            state=hub_state,
            nonce=nonce,
            challenge=challenge,
            login_hint=record["sub"] if cfg.hub_login_hint == "sub" else None,
            redirect_uri=hub_redirect(b),
        )
    except (HubUnavailable, HubLoginError) as exc:
        audit(
            "broker.consent.fail",
            vendor=vendor,
            sub=record["sub"],
            reason="hub_unavailable",
            security_event=False,
        )
        return problem(503, "hub-unavailable", str(exc))
    try:
        await b.coord.put_state(
            hub_state,
            {
                "leg": "hub",
                "txn_id": txn,
                "sub": record["sub"],
                "vendor": vendor,
                "scopes": record["scopes"],
                "nonce": nonce,
                "pkce_verifier": verifier,
                "binding": binding_hash(binding),
                "created_at": time.time(),
            },
        )
    except CoordinationUnavailable as exc:
        return problem(503, "coordination-unavailable", str(exc))
    audit("broker.consent.start", sub=record["sub"], vendor=vendor)
    resp = RedirectResponse(login_url, headers=BROWSER_HEADERS)
    resp.set_cookie(
        binding_cookie(vendor),
        binding,
        max_age=cfg.txn_ttl_s,
        path="/v1/callback",
        httponly=True,
        samesite="lax",
        secure=cfg.broker_public_url.startswith("https://"),
    )
    return resp


async def hub_callback(b: Broker, request: Request) -> Response:
    """The hub login came back: same browser, same user, then the vendor leg."""
    cfg = b.cfg
    q = request.query_params
    state = q.get("state", "")
    try:
        record = await b.coord.peek_state(state)
        if record is None or record.get("leg") != "hub":
            audit(
                "broker.consent.fail",
                vendor=HUB_LEG,
                reason="state_invalid_or_replayed",
                security_event=True,
            )
            return page("Invalid or expired sign-in state.", 400)
        vendor = record["vendor"]
        if not bound_to_this_browser(request, record):
            audit(
                "broker.consent.fail",
                vendor=vendor,
                sub=record["sub"],
                reason="browser_mismatch",
                leg="hub",
                security_event=True,
            )
            return page("This sign-in was started in a different browser.", 400)
        iss = q.get("iss")
        if iss is not None and iss != cfg.hub_issuer:
            audit(
                "broker.consent.fail",
                vendor=vendor,
                reason="iss_mismatch",
                leg="hub",
                security_event=True,
                iss_present=True,
            )
            return page("Issuer mismatch.", 400)
        record = await b.coord.consume_state(state)
        if record is None:
            audit(
                "broker.consent.fail",
                vendor=vendor,
                reason="state_invalid_or_replayed",
                security_event=True,
            )
            return page("Invalid or expired sign-in state.", 400)
    except CoordinationUnavailable:
        audit(
            "broker.consent.fail",
            vendor=HUB_LEG,
            reason="coordination_unavailable",
            security_event=False,
        )
        return page("Coordination store unavailable.", 503)
    if "error" in q:
        audit(
            "broker.consent.fail",
            vendor=vendor,
            sub=record["sub"],
            reason=q.get("error"),
            leg="hub",
            security_event=False,
        )
        return page("Sign-in failed.", 400)
    try:
        claims = await b.hub_login.exchange(
            code=q.get("code", ""),
            verifier=record["pkce_verifier"],
            redirect_uri=hub_redirect(b),
            nonce=record["nonce"],
        )
    except (HubLoginError, HubUnavailable) as exc:
        audit(
            "broker.consent.fail",
            vendor=vendor,
            sub=record["sub"],
            reason="hub_login_failed",
            error=str(exc),
            security_event=False,
        )
        return page("Sign-in failed.", 502)
    if claims["sub"] != record["sub"]:
        # The link was opened by someone other than the user it was
        # issued to: forwarded to a victim, or stolen from one.
        audit(
            "broker.consent.fail",
            vendor=vendor,
            sub=record["sub"],
            login_sub=claims["sub"],
            reason="login_sub_mismatch",
            security_event=True,
        )
        return page("This link was issued to a different user.", 403)
    return await start_vendor_leg(b, record)


async def start_vendor_leg(b: Broker, record: dict) -> Response:
    cfg, problem = b.cfg, b.problem
    vendor = record["vendor"]
    try:
        eps = await b.vendors.endpoints(vendor)
        creds = await b.vendors.read_client(vendor)
    except vendors_mod.VendorUnavailable as exc:
        audit(
            "broker.consent.fail",
            vendor=vendor,
            sub=record["sub"],
            reason="vendor_unavailable",
            security_event=False,
        )
        return problem(503, "vendor-unavailable", str(exc))
    except CustodyUnavailable as exc:
        audit(
            "broker.consent.fail",
            vendor=vendor,
            sub=record["sub"],
            reason="vault_unavailable",
            security_event=False,
        )
        return problem(503, "vault-unavailable", str(exc))
    if not creds or not creds.get("client_id"):
        audit(
            "broker.consent.fail",
            vendor=vendor,
            sub=record["sub"],
            reason="no_client_credential",
            security_event=False,
        )
        return problem(503, "vendor-unavailable", f"no client credential for {vendor}")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    try:
        await b.coord.put_state(
            state,
            {
                "leg": "vendor",
                "txn_id": record["txn_id"],
                "sub": record["sub"],
                "vendor": vendor,
                "pkce_verifier": verifier,
                "nonce": secrets.token_urlsafe(16),
                "issuer": eps.get("issuer"),
                "created_at": time.time(),
                # RFC 9207 §2.4: if the AS advertises iss support, a callback
                # that omits iss is a mix-up signal (see callback).
                "iss_required": bool(eps.get("authorization_response_iss_parameter_supported")),
                "binding": record["binding"],  # same browser as the hub leg
                "scopes": record["scopes"],
            },
        )  # ≤ registry ceiling (§4.2)
    except CoordinationUnavailable as exc:
        return problem(503, "coordination-unavailable", str(exc))
    params = {
        "client_id": creds["client_id"],
        "response_type": "code",
        "redirect_uri": f"{cfg.broker_public_url}/v1/callback/{vendor}",
        "scope": " ".join(record["scopes"]),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if resource := b.vendors.resource(vendor):
        params["resource"] = resource  # RFC 8707: token bound to this MCP server
    return RedirectResponse(
        f"{eps['authorization_endpoint']}?{urlencode(params)}", headers=BROWSER_HEADERS
    )


async def callback(b: Broker, vendor: str, request: Request) -> Response:
    """A consent leg came back: the hub (vendor "_hub") or a vendor."""
    cfg = b.cfg
    if vendor == HUB_LEG:
        return await hub_callback(b, request)
    q = request.query_params
    state = q.get("state", "")
    try:
        record = await b.coord.peek_state(state)
        if record is None or record["vendor"] != vendor or record.get("leg", "vendor") != "vendor":
            # §4.3/§10: state replay or mismatch is a SECURITY EVENT.
            audit(
                "broker.consent.fail",
                vendor=vendor,
                reason="state_invalid_or_replayed",
                security_event=True,
            )
            return page("Invalid or expired authorization state.", 400)
        # RFC 9207 mix-up defense: strict string comparison against the
        # issuer recorded at transaction creation; applies before any code
        # redemption — and before consumption, so a tampered callback does
        # not burn the state the legitimate one still needs.
        # When the vendor AS advertises
        # authorization_response_iss_parameter_supported (RFC 8414), an
        # authorization response with no iss is itself a mix-up signal
        # (RFC 9207 §2.4) and is rejected exactly like a mismatched one.
        iss = q.get("iss")
        if record["issuer"] is not None and (
            iss is None and record["iss_required"] or iss is not None and iss != record["issuer"]
        ):
            audit(
                "broker.consent.fail",
                vendor=vendor,
                reason="iss_mismatch",
                security_event=True,
                iss_present=iss is not None,
            )
            return page("Issuer mismatch.", 400)
        # Only the browser that started the flow may finish it (H1): a
        # vendor authorization completed elsewhere never redeems a code.
        if not bound_to_this_browser(request, record):
            audit(
                "broker.consent.fail",
                vendor=vendor,
                sub=record["sub"],
                reason="browser_mismatch",
                leg="vendor",
                security_event=True,
            )
            return page("This connection was started in a different browser.", 400)
        # Single-use consumption BEFORE redemption: exactly one callback
        # per state ever reaches the token endpoint.
        record = await b.coord.consume_state(state)
        if record is None:  # lost a consumption race — treat as replay
            audit(
                "broker.consent.fail",
                vendor=vendor,
                reason="state_invalid_or_replayed",
                security_event=True,
            )
            return page("Invalid or expired authorization state.", 400)
    except CoordinationUnavailable:
        audit(
            "broker.consent.fail",
            vendor=vendor,
            reason="coordination_unavailable",
            security_event=False,
        )
        return page("Coordination store unavailable.", 503)
    if "error" in q:
        # The raw vendor error goes to the audit line only; the browser
        # gets a constant page (no reflected attacker-controllable value).
        audit(
            "broker.consent.fail",
            vendor=vendor,
            sub=record["sub"],
            reason=q.get("error"),
            security_event=False,
        )
        return page("Authorization failed.", 400)
    # A grant the user asked to delete (parked REVOKE_PENDING) is revoked
    # and removed before this consent replaces it; otherwise the overwrite
    # would silently drop the pending revocation (review L3). Only that
    # case: revoking an ACTIVE or STALE predecessor could kill the NEW
    # grant at vendors that revoke per user and client (GitHub grant
    # deletion, many RFC 7009 servers), so those are simply overwritten.
    try:
        prior = await b.custody.read(vendor, record["sub"])
        if prior is not None and prior[0]["state"] == "REVOKE_PENDING":
            try:
                await b.vendors.revoke(vendor, prior[0])
                prior_outcome = "revoked"
            except vendors_mod.RevocationUnsupported:
                prior_outcome = "unsupported"
            except vendors_mod.VendorError:
                audit(
                    "broker.consent.fail",
                    vendor=vendor,
                    sub=record["sub"],
                    reason="prior_revocation_pending",
                    security_event=False,
                )
                return page(
                    "Your previous connection is still being revoked. Try again later.", 503
                )
            await b.custody.delete(vendor, record["sub"])
            await b.invalidate(vendor, record["sub"])
            audit(
                "broker.revoke",
                sub=record["sub"],
                vendor=vendor,
                outcome=prior_outcome,
                path="reconsent",
            )
    except CustodyUnavailable:
        audit(
            "broker.consent.fail",
            vendor=vendor,
            sub=record["sub"],
            reason="vault_unavailable",
            security_event=False,
        )
        return page("Credential store unavailable.", 503)
    try:
        tok = await b.vendors.exchange_code(
            vendor,
            q.get("code", ""),
            record["pkce_verifier"],
            f"{cfg.broker_public_url}/v1/callback/{vendor}",
        )
    except vendors_mod.VendorError as exc:
        audit(
            "broker.consent.fail",
            vendor=vendor,
            sub=record["sub"],
            reason=str(exc),
            security_event=False,
        )
        return page("Token exchange failed.", 502)
    # The code is spent from here on: nothing below may lose the new grant
    # silently. vendor_user_id is best-effort ("unknown" on any failure).
    vendor_uid = await b.vendors.vendor_user_id(vendor, tok["access_token"])
    ceiling = b.vendors.registry().get(vendor, {}).get("scope_ceiling")
    entry = entry_from_token_response(tok, 1, vendor_uid, record["scopes"], ceiling=ceiling)
    widened = refresh_mod.scope_widening(tok, ceiling)
    # Write under the entry lock: the sweeper's revocation retry holds it
    # between its version check and its (unconditional) custody delete.
    # The code is spent, so a lock that never frees never loses the grant.
    try:
        lock_token, _ = await b.coord.wait_refresh_lock(vendor, record["sub"], None)
    except CoordinationUnavailable:
        lock_token = None
    try:
        await b.custody.write(vendor, record["sub"], entry, cas=None)  # re-consent: gen=1
    except CustodyUnavailable:
        # Unstorable: revoke the fresh grant at the vendor (best effort) so
        # it is not orphaned there, then let the user retry the dance.
        try:
            await b.vendors.revoke(vendor, entry)
            revoked = True
        except vendors_mod.VendorError:
            revoked = False
        audit(
            "broker.consent.fail",
            vendor=vendor,
            sub=record["sub"],
            reason="post_redeem_failure",
            new_grant_revoked=revoked,
            security_event=False,
        )
        return page("Credential store unavailable.", 503)
    finally:
        if lock_token is not None:
            await b.coord.release_refresh_lock(vendor, record["sub"], lock_token)
    await b.invalidate(vendor, record["sub"])
    audit(
        "broker.consent.complete",
        sub=record["sub"],
        vendor=vendor,
        vendor_user_id=vendor_uid,
        **({"scope_widened": widened} if widened else {}),
    )
    return page("Connected — return to your client.")
