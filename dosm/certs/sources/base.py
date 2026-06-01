"""Cloud certificate-source abstraction.

A ``CertificateSource`` pulls certificate inventory from a cloud vault/cert
service (Azure Key Vault, AWS ACM, GCP Certificate Manager) and maps each cert
onto the shared ``CertInfo`` the certs dashboard already renders. Same adapter
shape as the monitoring/directory adapters elsewhere.

These sources are **opt-in** and require outbound cloud API calls — unlike the
local-first defaults. Cloud SDKs are optional extras, imported lazily by each
concrete adapter so the lean/air-gapped core stays dependency-free.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

from dosm.monitoring.adapters.base import CertInfo, cert_status


def ensure_utc(dt: datetime) -> datetime:
    """Normalize a cert's not-after to tz-aware UTC (SDKs vary)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class CertSourceError(Exception):
    """A cloud cert source could not be reached or authenticated."""


class MissingDependencyError(CertSourceError):
    """The provider's optional SDK isn't installed (e.g. ``pip install dosm[azure]``)."""


@dataclass
class RawCert:
    """Provider-neutral cert read by an adapter, before status/expiry mapping.

    Keeping the SDK-touching ``_list_raw`` separate from this mapping lets the
    adapters be unit-tested without the cloud SDKs (override ``_list_raw``)."""

    name: str
    not_after: datetime
    subject_cn: str = ""
    subject: str = ""
    issuer_cn: str = ""
    issuer: str = ""
    entity_url: str | None = None
    serial: str | None = None


def raw_to_certinfo(
    raw: RawCert,
    *,
    source_id: int,
    source_name: str,
    tool: str,
    warn_days: int,
    critical_days: int,
) -> CertInfo:
    status, days = cert_status(raw.not_after, warn_days, critical_days)
    cn = raw.subject_cn or raw.name
    return CertInfo(
        endpoint=f"{source_name}/{raw.name}",
        subject_cn=cn,
        subject=raw.subject or f"CN={cn}",
        issuer_cn=raw.issuer_cn,
        issuer=raw.issuer or raw.issuer_cn,
        not_after=raw.not_after,
        days_remaining=days,
        status=status,
        source_id=source_id,
        source_name=source_name,
        tool=tool,
        entity_url=raw.entity_url,
        serial=raw.serial,
    )


class CertificateSource(ABC):
    """Reads certificate inventory from one configured cloud source."""

    #: provider key, matches CertSource.provider
    provider: str = "unknown"

    def __init__(self, source_id: int, source_name: str) -> None:
        self.source_id = source_id
        self.source_name = source_name

    @abstractmethod
    async def fetch_certificates(
        self, warn_days: int = 30, critical_days: int = 14
    ) -> list[CertInfo]: ...

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """Return (ok, human-readable message) for the source-config 'Test' button."""


class CloudCertSource(CertificateSource):
    """Base for SDK-backed cloud sources. Subclasses implement the blocking
    ``_list_raw()`` (the only SDK-touching method); the base runs it off-thread
    and maps to ``CertInfo``. Tests override ``_list_raw`` to skip the SDK."""

    tool: str = "Cloud"

    def _list_raw(self) -> list[RawCert]:
        raise NotImplementedError

    async def fetch_certificates(
        self, warn_days: int = 30, critical_days: int = 14
    ) -> list[CertInfo]:
        loop = asyncio.get_running_loop()
        raws = await loop.run_in_executor(None, self._list_raw)
        return [
            raw_to_certinfo(
                r, source_id=self.source_id, source_name=self.source_name,
                tool=self.tool, warn_days=warn_days, critical_days=critical_days,
            )
            for r in raws
        ]

    async def test_connection(self) -> tuple[bool, str]:
        try:
            loop = asyncio.get_running_loop()
            raws = await loop.run_in_executor(None, self._list_raw)
            return True, f"Connected — {len(raws)} certificate(s) found"
        except MissingDependencyError as e:
            return False, str(e)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
