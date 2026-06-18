"""Phase 21b — Okta OIDC SSO, validated offline with a self-signed ID token."""
from __future__ import annotations

import time
from contextlib import contextmanager

import pytest
from authlib.jose import JsonWebKey, jwt
from fastapi.testclient import TestClient
from sqlalchemy import select

from dosm.auth import okta as okta_oidc
from dosm.config import RbacConfig
from dosm.models import User
from dosm.secrets import get_backend

ISSUER = "https://issuer.example.com"
CLIENT_ID = "client123"


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

def test_map_groups_highest_wins():
    rbac = RbacConfig(
        group_role_map={"Ops": "operator", "Admins": "admin", "Read": "viewer"},
        default_role="viewer",
    )
    assert okta_oidc.map_groups_to_role(["Ops", "Admins"], rbac) == "admin"
    assert okta_oidc.map_groups_to_role(["Ops", "Read"], rbac) == "operator"


def test_map_groups_default_and_unknown():
    rbac = RbacConfig(group_role_map={"Admins": "admin"}, default_role="viewer")
    assert okta_oidc.map_groups_to_role(["nope", "other"], rbac) == "viewer"
    assert okta_oidc.map_groups_to_role([], rbac) == "viewer"
    assert okta_oidc.map_groups_to_role(None, rbac) == "viewer"


def test_map_groups_denies_unmapped_when_default_none():
    rbac = RbacConfig(group_role_map={"Admins": "admin"}, default_role="none")
    # No mapped group → denied (None).
    assert okta_oidc.map_groups_to_role(["other"], rbac) is None
    assert okta_oidc.map_groups_to_role([], rbac) is None
    assert okta_oidc.map_groups_to_role(None, rbac) is None
    # A mapped group still grants its role.
    assert okta_oidc.map_groups_to_role(["Admins"], rbac) == "admin"


def test_map_groups_default_none_is_the_model_default():
    # Out of the box, RbacConfig denies unmapped users (group membership required).
    assert RbacConfig().default_role == "none"
    assert okta_oidc.map_groups_to_role(["whatever"], RbacConfig()) is None


# ---------------------------------------------------------------------------
# Token signing fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def signing_key():
    return JsonWebKey.generate_key("RSA", 2048, options={"kid": "testkid"}, is_private=True)


@pytest.fixture(scope="module")
def jwks_public(signing_key):
    return {"keys": [signing_key.as_dict(is_private=False)]}


def _make_id_token(signing_key, *, groups, nonce, sub="okta-sub-1", **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": sub,
        "email": "ssouser@example.com",
        "preferred_username": "ssouser",
        "name": "SSO User",
        "groups": groups,
        "nonce": nonce,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    header = {"alg": "RS256", "kid": "testkid"}
    return jwt.encode(header, claims, signing_key).decode()


def test_validate_id_token_roundtrip(signing_key, jwks_public):
    token = _make_id_token(signing_key, groups=["Admins"], nonce="n1")
    claims = okta_oidc.validate_id_token(
        token, jwks_public, issuer=ISSUER, client_id=CLIENT_ID, nonce="n1"
    )
    assert claims["sub"] == "okta-sub-1"


def test_validate_id_token_rejects_bad_nonce(signing_key, jwks_public):
    token = _make_id_token(signing_key, groups=["Admins"], nonce="n1")
    with pytest.raises(okta_oidc.OktaError):
        okta_oidc.validate_id_token(
            token, jwks_public, issuer=ISSUER, client_id=CLIENT_ID, nonce="WRONG"
        )


def test_validate_id_token_rejects_wrong_audience(signing_key, jwks_public):
    token = _make_id_token(signing_key, groups=["Admins"], nonce="n1", aud="someone-else")
    with pytest.raises(okta_oidc.OktaError):
        okta_oidc.validate_id_token(
            token, jwks_public, issuer=ISSUER, client_id=CLIENT_ID, nonce="n1"
        )


# ---------------------------------------------------------------------------
# End-to-end callback (network mocked, token real)
# ---------------------------------------------------------------------------

@contextmanager
def _okta_enabled(test_config, group_map, default_role="viewer"):
    okta, rbac = test_config.okta, test_config.rbac
    saved = (okta.enabled, okta.issuer, okta.client_id, dict(rbac.group_role_map), rbac.default_role)
    okta.enabled = True
    okta.issuer = ISSUER
    okta.client_id = CLIENT_ID
    rbac.group_role_map = dict(group_map)
    rbac.default_role = default_role
    try:
        yield
    finally:
        (okta.enabled, okta.issuer, okta.client_id, rbac.group_role_map, rbac.default_role) = saved


def _patch_network(monkeypatch, signing_key, jwks_public, *, groups, sub="okta-sub-1"):
    metadata = {
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
    }

    async def fake_metadata(_issuer):
        return metadata

    async def fake_jwks(_uri):
        return jwks_public

    async def fake_exchange(_meta, **kwargs):
        # nonce was forced deterministic via new_state below
        return {"id_token": _make_id_token(signing_key, groups=groups, nonce="fixedval", sub=sub)}

    monkeypatch.setattr(okta_oidc, "fetch_metadata", fake_metadata)
    monkeypatch.setattr(okta_oidc, "fetch_jwks", fake_jwks)
    monkeypatch.setattr(okta_oidc, "exchange_code", fake_exchange)
    # Force deterministic state/nonce so the issued token's nonce matches the
    # value stashed in the session by /auth/okta/login.
    monkeypatch.setattr(okta_oidc, "new_state", lambda: "fixedval")
    monkeypatch.setattr(okta_oidc, "new_pkce_pair", lambda: ("verifier", "challenge"))


def test_okta_callback_provisions_user(
    app, test_config, session_factory, monkeypatch, signing_key, jwks_public
):
    get_backend(test_config).set_str("okta/client_secret", "shh")
    _patch_network(monkeypatch, signing_key, jwks_public, groups=["DOSM-Admins"])

    with _okta_enabled(test_config, {"DOSM-Admins": "admin"}):
        client = TestClient(app, raise_server_exceptions=True)
        r1 = client.get("/auth/okta/login", follow_redirects=False)
        assert r1.status_code == 303
        r2 = client.get(
            "/auth/okta/callback?code=abc&state=fixedval", follow_redirects=False
        )
        assert r2.status_code == 303, r2.text
        # Authenticated session now usable.
        assert client.get("/hosts", follow_redirects=False).status_code == 200

    with session_factory() as s:
        u = s.execute(select(User).where(User.okta_sub == "okta-sub-1")).scalar_one()
        assert u.role == "admin"
        assert u.auth_provider == "okta"
        assert u.email == "ssouser@example.com"


def test_okta_callback_denies_user_in_no_mapped_group(
    app, test_config, session_factory, monkeypatch, signing_key, jwks_public
):
    get_backend(test_config).set_str("okta/client_secret", "shh")
    # The user's groups don't intersect the mapping, and default is deny.
    _patch_network(monkeypatch, signing_key, jwks_public,
                   groups=["Some-Other-Group"], sub="denied-sub-1")

    with _okta_enabled(test_config, {"DOSM-Admins": "admin"}, default_role="none"):
        client = TestClient(app, raise_server_exceptions=True)
        client.get("/auth/okta/login", follow_redirects=False)
        r = client.get("/auth/okta/callback?code=abc&state=fixedval", follow_redirects=False)
        assert r.status_code == 403
        assert "group granted DOSM access" in r.text
        # No session was established.
        assert client.get("/hosts", follow_redirects=False).status_code in (303, 401)

    # And no user row was provisioned for the denied subject.
    with session_factory() as s:
        assert s.execute(select(User).where(User.okta_sub == "denied-sub-1")).scalar_one_or_none() is None


def test_okta_role_recomputed_on_each_login(
    app, test_config, session_factory, monkeypatch, signing_key, jwks_public
):
    get_backend(test_config).set_str("okta/client_secret", "shh")

    # First login: member of a viewer-mapped group only.
    _patch_network(monkeypatch, signing_key, jwks_public, groups=["DOSM-Viewers"])
    with _okta_enabled(test_config, {"DOSM-Viewers": "viewer", "DOSM-Admins": "admin"}):
        c1 = TestClient(app, raise_server_exceptions=True)
        c1.get("/auth/okta/login", follow_redirects=False)
        c1.get("/auth/okta/callback?code=abc&state=fixedval", follow_redirects=False)
    with session_factory() as s:
        assert s.execute(select(User).where(User.okta_sub == "okta-sub-1")).scalar_one().role == "viewer"

    # Second login after being added to the admin group: role upgrades.
    _patch_network(monkeypatch, signing_key, jwks_public, groups=["DOSM-Viewers", "DOSM-Admins"])
    with _okta_enabled(test_config, {"DOSM-Viewers": "viewer", "DOSM-Admins": "admin"}):
        c2 = TestClient(app, raise_server_exceptions=True)
        c2.get("/auth/okta/login", follow_redirects=False)
        c2.get("/auth/okta/callback?code=abc&state=fixedval", follow_redirects=False)
    with session_factory() as s:
        assert s.execute(select(User).where(User.okta_sub == "okta-sub-1")).scalar_one().role == "admin"


def test_local_login_works_when_okta_enabled(app, test_config):
    with _okta_enabled(test_config, {"DOSM-Admins": "admin"}):
        c = TestClient(app, raise_server_exceptions=True)
        r = c.post(
            "/login",
            data={"username": "testadmin", "password": "testpass", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        # And the login page advertises the Okta button.
        page = c.get("/login")
        assert "Sign in with Okta" in page.text


def test_okta_routes_404_when_disabled(app, test_config):
    # test_config defaults okta.enabled=False
    c = TestClient(app, raise_server_exceptions=True)
    assert c.get("/auth/okta/login", follow_redirects=False).status_code == 404
