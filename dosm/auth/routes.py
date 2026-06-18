from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth import okta as okta_oidc
from dosm.auth.passwords import verify_password
from dosm.db import get_session
from dosm.models import AuditLog, User
from dosm.secrets import SecretNotFound, get_backend

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str = "/") -> HTMLResponse:
    okta = request.app.state.config.okta
    return _templates(request).TemplateResponse(
        request,
        "auth/login.html",
        {
            "error": None,
            "next": next,
            "username": "",
            "okta_enabled": okta.enabled,
        },
    )


@router.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_session),
):
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return _templates(request).TemplateResponse(
            request,
            "auth/login.html",
            {
                "error": "Invalid credentials.",
                "next": next,
                "username": username,
                "okta_enabled": request.app.state.config.okta.enabled,
            },
            status_code=401,
        )
    request.session["user_id"] = user.id
    db.add(
        AuditLog(
            actor_id=user.id,
            action="auth.login",
            target=f"user:{user.id}",
            ip=request.client.host if request.client else None,
        )
    )
    return RedirectResponse(next or "/", status_code=303)


@router.post("/logout", include_in_schema=False)
async def logout(request: Request, db: Session = Depends(get_session)):
    uid = request.session.get("user_id")
    request.session.clear()
    if uid is not None:
        db.add(AuditLog(actor_id=uid, action="auth.logout", target=f"user:{uid}"))
        db.commit()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------------
# Okta OIDC SSO (Phase 21b)
# ---------------------------------------------------------------------------

def _require_okta_enabled(request: Request):
    cfg = request.app.state.config
    if not cfg.okta.enabled:
        raise HTTPException(status_code=404, detail="Okta SSO is not enabled")
    return cfg


@router.get("/auth/okta/login", include_in_schema=False)
async def okta_login(request: Request, next: str = "/"):
    cfg = _require_okta_enabled(request)
    okta = cfg.okta
    if not okta.issuer or not okta.client_id:
        raise HTTPException(500, "Okta issuer/client_id not configured")
    try:
        metadata = await okta_oidc.fetch_metadata(okta.issuer)
    except Exception as e:
        raise HTTPException(502, f"Okta discovery failed: {e}") from e

    state = okta_oidc.new_state()
    nonce = okta_oidc.new_state()
    verifier, challenge = okta_oidc.new_pkce_pair()
    # Stash the one-time handshake values in the session for the callback.
    request.session["okta_state"] = state
    request.session["okta_nonce"] = nonce
    request.session["okta_verifier"] = verifier
    request.session["okta_next"] = next or "/"

    redirect_uri = okta_oidc.redirect_uri_for(str(request.base_url), okta)
    url = okta_oidc.build_authorize_url(
        metadata,
        client_id=okta.client_id,
        redirect_uri=redirect_uri,
        scopes=okta.scopes,
        state=state,
        nonce=nonce,
        code_challenge=challenge,
    )
    return RedirectResponse(url, status_code=303)


@router.get("/auth/okta/callback", include_in_schema=False)
async def okta_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_session),
):
    cfg = _require_okta_enabled(request)
    okta = cfg.okta

    if error:
        raise HTTPException(400, f"Okta returned an error: {error}")
    if not code or not state:
        raise HTTPException(400, "missing code/state")

    # Validate state against the value we stashed; consume the handshake values.
    expected_state = request.session.pop("okta_state", None)
    nonce = request.session.pop("okta_nonce", None)
    verifier = request.session.pop("okta_verifier", None)
    next_url = request.session.pop("okta_next", "/") or "/"
    if not expected_state or state != expected_state or not verifier:
        raise HTTPException(400, "invalid or expired SSO state; please retry")

    try:
        client_secret = get_backend(cfg).get_str("okta/client_secret")
    except SecretNotFound as e:
        raise HTTPException(500, "okta/client_secret is not set in the secrets backend") from e

    redirect_uri = okta_oidc.redirect_uri_for(str(request.base_url), okta)
    try:
        metadata = await okta_oidc.fetch_metadata(okta.issuer)
        token_resp = await okta_oidc.exchange_code(
            metadata,
            client_id=okta.client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=verifier,
        )
        id_token = token_resp.get("id_token")
        if not id_token:
            raise okta_oidc.OktaError("token response had no id_token")
        jwks = await okta_oidc.fetch_jwks(metadata["jwks_uri"])
        claims = okta_oidc.validate_id_token(
            id_token, jwks, issuer=okta.issuer, client_id=okta.client_id, nonce=nonce
        )
    except okta_oidc.OktaError as e:
        raise HTTPException(401, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"Okta sign-in failed: {e}") from e

    identity = okta_oidc.extract_identity(claims, okta.groups_claim)
    if not identity["sub"]:
        raise HTTPException(401, "ID token missing subject")
    role = okta_oidc.map_groups_to_role(identity["groups"], cfg.rbac)

    # Deny anyone who isn't a member of a group granted a DOSM role. We do NOT
    # provision a user row in this case — group membership is required for access.
    if role is None:
        db.add(
            AuditLog(
                actor_id=None,
                action="auth.login.okta.denied",
                target=f"okta_sub:{identity['sub']}",
                details=f"email={identity['email']} groups={len(identity['groups'])} (no mapped group)",
                ip=request.client.host if request.client else None,
            )
        )
        db.commit()
        return _templates(request).TemplateResponse(
            request,
            "auth/login.html",
            {
                "error": "Your account isn't a member of any group granted DOSM access. "
                         "Contact an administrator.",
                "next": "/",
                "username": "",
                "okta_enabled": True,
            },
            status_code=403,
        )

    user, prev_role = okta_oidc.provision_user(
        db,
        okta_sub=identity["sub"],
        username=identity["username"],
        email=identity["email"],
        display_name=identity["display_name"],
        role=role,
    )
    if not user.is_active:
        raise HTTPException(403, "this account is disabled")

    request.session["user_id"] = user.id
    db.add(
        AuditLog(
            actor_id=user.id,
            action="auth.login.okta",
            target=f"user:{user.id}",
            details=f"role={role} groups={len(identity['groups'])}",
            ip=request.client.host if request.client else None,
        )
    )
    if prev_role is not None and prev_role != role:
        db.add(
            AuditLog(
                actor_id=user.id,
                action="rbac.role_assigned",
                target=f"user:{user.id}",
                details=f"{prev_role} -> {role} (from Okta groups)",
            )
        )
    db.commit()
    return RedirectResponse(next_url, status_code=303)
