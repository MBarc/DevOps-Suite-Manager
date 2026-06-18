"""Cloud certificate sources - Azure Key Vault, AWS ACM, GCP Certificate
Manager (and a Mock for dev/tests). Factory dispatches by ``CertSource.provider``.

``SUPPORTED_PROVIDERS`` drives the source-config dropdown. Cloud SDKs are
optional extras imported lazily by each adapter.
"""
from __future__ import annotations

import json

from dosm.certs.sources.base import (
    CertificateSource,
    CertSourceError,
    CloudCertSource,
    MissingDependencyError,
)
from dosm.config import Config
from dosm.models import CertSource

# provider key -> human label (drives the source-config dropdown + route
# validation). "mock" is intentionally excluded - it stays available to the
# factory/tests but is not a user-selectable provider.
SUPPORTED_PROVIDERS: dict[str, str] = {
    "azure_kv": "Azure Key Vault",
    "aws_acm": "AWS Certificate Manager (ACM)",
    "gcp_certmgr": "GCP Certificate Manager",
}

# provider -> cloud credential kind expected in 'profile' auth mode
PROVIDER_CRED_KIND = {
    "azure_kv": "azure_sp",
    "aws_acm": "aws_keys",
    "gcp_certmgr": "gcp_sa",
}


def get_cert_source(source: CertSource, cfg: Config | None = None) -> CertificateSource:
    """Build the adapter for a configured ``CertSource`` row.

    In ``profile`` auth mode the attached credential profile is resolved from the
    secrets backend (needs ``cfg``); ``ambient`` mode leaves credentials to the
    cloud SDK's default chain (managed identity / instance role / workload id).
    """
    provider = source.provider
    if provider == "mock":
        from dosm.certs.sources.mock import MockCertSource

        return MockCertSource(source.id, source.name)

    config = json.loads(source.config_json or "{}")
    credential: dict | None = None
    if source.auth_mode == "profile":
        if cfg is None:
            raise CertSourceError("resolving a credential profile requires config")
        from dosm.certs.sources.creds import resolve_cloud_credential

        credential = resolve_cloud_credential(cfg, source.credential)

    if provider == "azure_kv":
        from dosm.certs.sources.azure_kv import AzureKeyVaultSource

        return AzureKeyVaultSource(
            source.id, source.name, vault_url=config.get("vault_url", ""), credential=credential
        )
    if provider == "aws_acm":
        from dosm.certs.sources.aws_acm import AwsAcmSource

        return AwsAcmSource(
            source.id, source.name, region=config.get("region", ""), credential=credential
        )
    if provider == "gcp_certmgr":
        from dosm.certs.sources.gcp_certmgr import GcpCertManagerSource

        return GcpCertManagerSource(
            source.id, source.name, project=config.get("project", ""),
            location=config.get("location", "global"), credential=credential,
        )
    raise CertSourceError(f"unknown cert-source provider {provider!r}")


__all__ = [
    "CertificateSource",
    "CloudCertSource",
    "CertSourceError",
    "MissingDependencyError",
    "SUPPORTED_PROVIDERS",
    "PROVIDER_CRED_KIND",
    "get_cert_source",
]
