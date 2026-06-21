"""Phase 19 foundation: cloud cert-source abstraction + Mock + UI wiring."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import select

from dosm.auth.passwords import hash_password
from dosm.certs.sources import get_cert_source
from dosm.certs.sources.mock import MockCertSource
from dosm.models import CertSource, User


# ── unit: Mock source + factory ──────────────────────────────────────────────
def test_mock_cert_source_statuses():
    certs = asyncio.run(MockCertSource(1, "mock1").fetch_certificates(warn_days=30, critical_days=14))
    by = {c.subject_cn: c for c in certs}
    assert by["decommissioned-svc"].status == "expired"   # -12d
    assert by["legacy-app-cert"].status == "critical"     # 5d
    assert by["internal-wildcard"].status == "warn"       # 18d
    assert by["api-gateway-tls"].status == "ok"           # 95d
    assert all(c.source_name == "mock1" and c.tool == "Mock Vault" for c in certs)


def test_factory_builds_mock():
    src = get_cert_source(CertSource(id=3, name="m", provider="mock",
                                     config_json="{}", auth_mode="profile", enabled=True))
    assert isinstance(src, MockCertSource)


# ── integration: CRUD + vault section ───────────────────────────────────────
def _sid(session_factory, name):
    with session_factory() as s:
        return s.execute(select(CertSource).where(CertSource.name == name)).scalar_one().id


def test_cert_source_crud_and_vault_section(auth_client, session_factory):
    # mock isn't user-selectable, so seed it directly for the vault-section flow.
    with session_factory() as s:
        from sqlalchemy import text
        tid = s.execute(text("SELECT id FROM tenants WHERE slug='default'")).scalar_one()
        s.add(CertSource(name="mock-vault", provider="mock", config_json="{}",
                         auth_mode="ambient", enabled=True, tenant_id=tid))
        s.commit()
    sid = _sid(session_factory, "mock-vault")
    try:
        # listed on the sources page
        page = auth_client.get("/certs/sources")
        assert page.status_code == 200 and "mock-vault" in page.text
        # test endpoint reports OK
        t = auth_client.post(f"/certs/sources/{sid}/test")
        assert t.status_code == 200 and t.json()["ok"] is True
        # vault certs surface on the certs page
        certs = auth_client.get("/certs")
        assert "Vault certificates" in certs.text
        assert "api-gateway-tls" in certs.text and "decommissioned-svc" in certs.text
        # toggle off -> disabled
        auth_client.post(f"/certs/sources/{sid}/toggle", follow_redirects=False)
        with session_factory() as s:
            assert s.get(CertSource, sid).enabled is False
    finally:
        auth_client.post(f"/certs/sources/{sid}/delete", follow_redirects=False)
    with session_factory() as s:
        assert s.get(CertSource, sid) is None


# ── cloud adapters (SDK-free: override _list_raw / assert graceful degrade) ──
def test_cloud_adapter_maps_raw_to_certinfo():
    from datetime import UTC, datetime, timedelta

    from dosm.certs.sources.azure_kv import AzureKeyVaultSource
    from dosm.certs.sources.base import RawCert

    src = AzureKeyVaultSource(7, "prod-kv", vault_url="https://x.vault.azure.net/")
    src._list_raw = lambda: [
        RawCert(name="web", not_after=datetime.now(UTC) + timedelta(days=3),
                subject_cn="web.example.com", issuer_cn="DigiCert"),
        RawCert(name="old", not_after=datetime.now(UTC) - timedelta(days=2), subject_cn="old.example.com"),
    ]
    certs = asyncio.run(src.fetch_certificates(warn_days=30, critical_days=14))
    by = {c.subject_cn: c for c in certs}
    assert by["web.example.com"].status == "critical"
    assert by["web.example.com"].tool == "Azure Key Vault"
    assert by["web.example.com"].source_name == "prod-kv"
    assert by["old.example.com"].status == "expired"


def test_cloud_adapter_missing_sdk_degrades():
    from dosm.certs.sources.aws_acm import AwsAcmSource

    ok, msg = asyncio.run(AwsAcmSource(1, "acm", region="us-east-1").test_connection())
    assert ok is False and "dosm[aws]" in msg


def test_resolve_cloud_credentials(test_config):
    from dosm.certs.sources.creds import resolve_cloud_credential
    from dosm.models import Credential
    from dosm.secrets import get_backend

    get_backend(test_config).set_str("cloud/az", "az-secret")
    get_backend(test_config).set_str("cloud/aws", "aws-secret")
    get_backend(test_config).set_str("cloud/gcp", '{"type":"service_account"}')

    az = resolve_cloud_credential(test_config, Credential(
        name="az", kind="azure_sp", username="client-1", domain="tenant-9", secret_ref="cloud/az"))
    assert az == {"tenant_id": "tenant-9", "client_id": "client-1", "client_secret": "az-secret"}

    aws = resolve_cloud_credential(test_config, Credential(
        name="aws", kind="aws_keys", username="AKIA123", secret_ref="cloud/aws"))
    assert aws == {"access_key_id": "AKIA123", "secret_access_key": "aws-secret"}

    gcp = resolve_cloud_credential(test_config, Credential(
        name="gcp", kind="gcp_sa", secret_ref="cloud/gcp"))
    assert gcp == {"service_account_json": '{"type":"service_account"}'}


def test_factory_builds_cloud_adapter_ambient(test_config):
    import json

    from dosm.certs.sources import get_cert_source
    from dosm.certs.sources.aws_acm import AwsAcmSource

    src = get_cert_source(CertSource(
        id=9, name="acm", provider="aws_acm",
        config_json=json.dumps({"region": "eu-west-1"}), auth_mode="ambient", enabled=True), test_config)
    assert isinstance(src, AwsAcmSource) and src.region == "eu-west-1" and src.credential is None


def test_route_create_cloud_and_rejects_mock(auth_client, session_factory):
    r = auth_client.post("/certs/sources/new",
                         data={"name": "az-kv", "provider": "azure_kv", "auth_mode": "ambient",
                               "vault_url": "https://x.vault.azure.net/"},
                         follow_redirects=False)
    assert r.status_code == 303
    sid = _sid(session_factory, "az-kv")
    try:
        bad = auth_client.post("/certs/sources/new",
                               data={"name": "nope", "provider": "mock"}, follow_redirects=False)
        assert bad.status_code == 400  # mock not a user-selectable provider
    finally:
        auth_client.post(f"/certs/sources/{sid}/delete", follow_redirects=False)


def test_cert_sources_admin_gated(app, session_factory):
    with session_factory() as s:
        if not s.execute(select(User).where(User.username == "cs-op")).scalar_one_or_none():
            from sqlalchemy import text
            tid = s.execute(text("SELECT id FROM tenants WHERE slug='default'")).scalar_one()
            s.add(User(username="cs-op", password_hash=hash_password("pw"),
                       role="operator", tenant_id=tid, is_active=True))
            s.commit()
    c = TestClient(app)
    c.post("/login", data={"username": "cs-op", "password": "pw", "next": "/"}, follow_redirects=False)
    assert c.get("/certs/sources").status_code == 403
