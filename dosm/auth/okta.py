"""Okta OIDC single sign-on.

Authentication via Okta; authorization via the ID token's ``groups`` claim
mapped to a DOSM role. The flow is deliberately split into **pure** functions
(role mapping, claim extraction, JIT provisioning, ID-token validation against a
supplied JWKS) and **network** helpers (discovery, token exchange, JWKS fetch),
so the security-critical parts are unit-testable offline with a self-signed
token + fixture JWKS - no live Okta needed.

The client secret is read from the secrets backend (``okta/client_secret``),
never from config.yaml.
"""
from __future__ import annotations

import base64
import hashlib
import secrets as _secrets
from datetime import UTC, datetime

import httpx
from authlib.jose import JsonWebKey, jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.deps import ROLE_RANK
from dosm.config import OktaConfig, RbacConfig
from dosm.models import User

# SSO users never log in with a password; this sentinel hash can't be produced
# by bcrypt, so verify_password() against it always fails. Keeps the column
# NOT NULL without a SQLite ALTER (see migrations / Phase 21b note).
SENTINEL_PASSWORD_HASH = "!okta"


class OktaError(Exception):
    """Any failure in the OIDC handshake or token validation."""


# ---------------------------------------------------------------------------
# Pure logic (unit-tested offline)
# ---------------------------------------------------------------------------

def map_groups_to_role(groups, rbac: RbacConfig) -> str | None:
    """Map a user's group memberships to a DOSM role. Highest mapped role wins.

    A user in **no** mapped group falls back to ``rbac.default_role`` - unless
    that is set to deny (``"none"`` / unset / any non-role value), in which case
    this returns ``None`` meaning *access denied*. That's the secure default:
    only members of a group that's been granted a DOSM role can sign in.
    """
    best_role: str | None = None
    best_rank = -1
    for g in groups or []:
        role = rbac.group_role_map.get(g)
        if role is None:
            continue
        rank = ROLE_RANK.get(role, -1)
        if rank > best_rank:
            best_role, best_rank = role, rank
    if best_role is not None:
        return best_role
    # No mapped group matched - grant the default role only if it's a real role.
    return rbac.default_role if rbac.default_role in ROLE_RANK else None


def extract_identity(claims: dict, groups_claim: str) -> dict:
    """Pull the fields DOSM cares about out of validated ID-token claims."""
    return {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "username": (
            claims.get("preferred_username")
            or claims.get("email")
            or claims.get("sub")
        ),
        "display_name": claims.get("name"),
        "groups": claims.get(groups_claim) or [],
    }


def _unique_username(db: Session, desired: str, okta_sub: str) -> str:
    """Pick a username that doesn't collide with a *different* account. If the
    desired name is taken by someone who isn't this Okta subject, suffix it."""
    base = (desired or okta_sub or "user").strip() or "user"
    existing = db.execute(select(User).where(User.username == base)).scalar_one_or_none()
    if existing is None or existing.okta_sub == okta_sub:
        return base
    suffix = (okta_sub or _secrets.token_hex(3))[-6:]
    return f"{base}-{suffix}"


def provision_user(
    db: Session,
    *,
    okta_sub: str,
    username: str,
    email: str | None,
    display_name: str | None,
    role: str,
) -> tuple[User, str | None]:
    """JIT-create or update the local mirror of an Okta user. Returns the user
    and the *previous* role (or None if newly created) so the caller can audit a
    role change. The role is recomputed from the claim on every login, so AD
    group changes take effect at next sign-in."""
    user = db.execute(select(User).where(User.okta_sub == okta_sub)).scalar_one_or_none()
    prev_role: str | None = None
    if user is None:
        user = User(
            username=_unique_username(db, username, okta_sub),
            password_hash=SENTINEL_PASSWORD_HASH,
            role=role,
            auth_provider="okta",
            okta_sub=okta_sub,
            is_active=True,
        )
        db.add(user)
    else:
        prev_role = user.role
        user.role = role
        user.auth_provider = "okta"
    user.email = email
    user.display_name = display_name
    user.last_login = datetime.now(UTC)
    db.flush()
    return user, prev_role


def validate_id_token(
    id_token: str, jwks: dict, *, issuer: str, client_id: str, nonce: str | None
) -> dict:
    """Verify an ID token's signature against ``jwks`` and validate its standard
    claims (iss/aud/exp) plus the nonce. Returns the claims on success; raises
    OktaError otherwise. No network access - ``jwks`` is supplied by the caller."""
    try:
        key_set = JsonWebKey.import_key_set(jwks)
        claims = jwt.decode(
            id_token,
            key_set,
            claims_options={
                "iss": {"essential": True, "value": issuer},
                "aud": {"essential": True, "value": client_id},
            },
        )
        claims.validate()  # exp / iat / nbf / iss / aud
    except Exception as e:  # authlib raises a variety of error types
        raise OktaError(f"ID token validation failed: {e}") from e
    if nonce is not None and claims.get("nonce") != nonce:
        raise OktaError("ID token nonce mismatch")
    return dict(claims)


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def new_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(_secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def new_state() -> str:
    return _secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# Network helpers (mocked in tests)
# ---------------------------------------------------------------------------

def _discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


async def fetch_metadata(issuer: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(_discovery_url(issuer))
        r.raise_for_status()
        return r.json()


async def fetch_jwks(jwks_uri: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(jwks_uri)
        r.raise_for_status()
        return r.json()


def build_authorize_url(
    metadata: dict,
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": " ".join(scopes),
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{metadata['authorization_endpoint']}?{urlencode(params)}"


async def exchange_code(
    metadata: dict,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(metadata["token_endpoint"], data=data)
    if r.status_code != 200:
        raise OktaError(f"token exchange failed ({r.status_code}): {r.text}")
    return r.json()


def redirect_uri_for(base_url: str, cfg: OktaConfig) -> str:
    """Compose the registered redirect URI from the request's base URL."""
    return base_url.rstrip("/") + cfg.redirect_path
