"""GCP Certificate Manager source. SDK extra: ``pip install 'dosm[gcp]'``."""
from __future__ import annotations

from datetime import datetime

from dosm.certs.sources.base import (
    CertSourceError,
    CloudCertSource,
    MissingDependencyError,
    RawCert,
    ensure_utc,
)


class GcpCertManagerSource(CloudCertSource):
    provider = "gcp_certmgr"
    tool = "GCP Certificate Manager"

    def __init__(self, source_id: int, source_name: str, *, project: str,
                 location: str = "global", credential: dict | None = None) -> None:
        super().__init__(source_id, source_name)
        self.project = project
        self.location = location or "global"
        self.credential = credential  # dict (profile) or None (workload identity)

    def _client(self):
        try:
            from google.cloud import certificate_manager_v1
        except ImportError as e:
            raise MissingDependencyError(
                "GCP SDK not installed — run: pip install 'dosm[gcp]'"
            ) from e
        if not self.project:
            raise CertSourceError("GCP Certificate Manager source needs a project")
        if self.credential:
            import json

            from google.oauth2 import service_account
            info = json.loads(self.credential.get("service_account_json") or "{}")
            creds = service_account.Credentials.from_service_account_info(info)
            return certificate_manager_v1.CertificateManagerClient(credentials=creds)
        return certificate_manager_v1.CertificateManagerClient()

    def _list_raw(self) -> list[RawCert]:
        client = self._client()
        parent = f"projects/{self.project}/locations/{self.location}"
        out: list[RawCert] = []
        for cert in client.list_certificates(parent=parent):
            expire = getattr(cert, "expire_time", None)
            if expire is None:
                continue
            # proto-plus exposes Timestamp as a datetime; tolerate a raw Timestamp.
            not_after = expire if isinstance(expire, datetime) else expire.ToDatetime()
            short = cert.name.rsplit("/", 1)[-1]
            sans = list(getattr(cert, "san_dnsnames", []) or [])
            cn = sans[0] if sans else short
            out.append(RawCert(
                name=short, not_after=ensure_utc(not_after),
                subject_cn=cn, subject=", ".join(sans),
                entity_url="https://console.cloud.google.com/security/ccm/list/certificates",
            ))
        return out
