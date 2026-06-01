"""Fixture-backed cert source for dev/tests and exercising the UI without a
real cloud account (mirrors the directory ``MockSource`` pattern)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dosm.certs.sources.base import CertificateSource
from dosm.monitoring.adapters.base import CertInfo, cert_status

# (name, days-until-expiry, issuer)
_FIXTURES = [
    ("api-gateway-tls", 95, "DigiCert Global G2"),
    ("internal-wildcard", 18, "Lets Encrypt R3"),
    ("legacy-app-cert", 5, "Internal CA"),
    ("decommissioned-svc", -12, "Internal CA"),
]


class MockCertSource(CertificateSource):
    provider = "mock"

    def _certs(self, warn_days: int, critical_days: int) -> list[CertInfo]:
        now = datetime.now(UTC)
        out: list[CertInfo] = []
        for name, days, issuer in _FIXTURES:
            not_after = now + timedelta(days=days)
            status, remaining = cert_status(not_after, warn_days, critical_days)
            out.append(CertInfo(
                endpoint=f"{self.source_name}/{name}",
                subject_cn=name, subject=f"CN={name}",
                issuer_cn=issuer, issuer=f"CN={issuer}",
                not_after=not_after, days_remaining=remaining, status=status,
                source_id=self.source_id, source_name=self.source_name,
                tool="Mock Vault", entity_url=None,
            ))
        return out

    async def fetch_certificates(
        self, warn_days: int = 30, critical_days: int = 14
    ) -> list[CertInfo]:
        return self._certs(warn_days, critical_days)

    async def test_connection(self) -> tuple[bool, str]:
        return True, f"Mock source OK — {len(_FIXTURES)} fixture certificates"
