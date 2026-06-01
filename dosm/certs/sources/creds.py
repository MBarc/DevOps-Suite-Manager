"""Resolve a cloud credential profile into provider-specific auth material.

Azure SP and AWS map onto the existing Credential fields (no JSON): Azure uses
domain=tenant, username=client_id, secret=client_secret; AWS uses
username=access_key_id, secret=secret_access_key. GCP's service-account key is a
JSON blob stored as the secret. The secret value comes from the secrets backend.
"""
from __future__ import annotations

from dosm.certs.sources.base import CertSourceError
from dosm.config import Config
from dosm.models import Credential
from dosm.secrets import SecretNotFound, get_backend

CLOUD_CRED_KINDS = ("azure_sp", "aws_keys", "gcp_sa")


def resolve_cloud_credential(cfg: Config, cred: Credential | None) -> dict:
    """Return provider-specific creds for ``cred``; raise if unusable.

    Shapes:
      azure_sp -> {tenant_id, client_id, client_secret}
      aws_keys -> {access_key_id, secret_access_key}
      gcp_sa   -> {service_account_json}
    """
    if cred is None:
        raise CertSourceError("source is in 'profile' auth mode but has no credential profile attached")
    if cred.kind not in CLOUD_CRED_KINDS:
        raise CertSourceError(
            f"credential {cred.name!r} (kind {cred.kind!r}) is not a cloud credential; "
            f"expected one of {', '.join(CLOUD_CRED_KINDS)}"
        )
    try:
        secret = get_backend(cfg).get_str(cred.secret_ref)
    except SecretNotFound:
        secret = ""
    if cred.kind == "azure_sp":
        return {
            "tenant_id": cred.domain or "",
            "client_id": cred.username or "",
            "client_secret": secret,
        }
    if cred.kind == "aws_keys":
        return {"access_key_id": cred.username or "", "secret_access_key": secret}
    return {"service_account_json": secret}  # gcp_sa
