"""Azure Key Vault certificate source. SDK extra: ``pip install 'dosm[azure]'``."""
from __future__ import annotations

from dosm.certs.sources.base import (
    CertSourceError,
    CloudCertSource,
    MissingDependencyError,
    RawCert,
    ensure_utc,
)


def _cn(name) -> str:
    """First CommonName RDN from an x509 Name, else ''."""
    try:
        from cryptography.x509.oid import NameOID
        attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        return attrs[0].value if attrs else ""
    except Exception:
        return ""


class AzureKeyVaultSource(CloudCertSource):
    provider = "azure_kv"
    tool = "Azure Key Vault"

    def __init__(self, source_id: int, source_name: str, *, vault_url: str,
                 credential: dict | None = None) -> None:
        super().__init__(source_id, source_name)
        self.vault_url = vault_url
        self.credential = credential  # dict (profile) or None (ambient identity)

    def _client(self):
        try:
            from azure.identity import ClientSecretCredential, DefaultAzureCredential
            from azure.keyvault.certificates import CertificateClient
        except ImportError as e:
            raise MissingDependencyError(
                "Azure SDK not installed - run: pip install 'dosm[azure]'"
            ) from e
        if not self.vault_url:
            raise CertSourceError("Azure Key Vault source needs a vault URL")
        if self.credential:
            c = self.credential
            token = ClientSecretCredential(
                c.get("tenant_id", ""), c.get("client_id", ""), c.get("client_secret", "")
            )
        else:
            token = DefaultAzureCredential()
        return CertificateClient(vault_url=self.vault_url, credential=token)

    def _list_raw(self) -> list[RawCert]:
        from cryptography import x509  # already present via asyncssh

        client = self._client()
        out: list[RawCert] = []
        for props in client.list_properties_of_certificates():
            name = props.name
            not_after = props.expires_on
            subject = issuer = subject_cn = issuer_cn = ""
            try:
                full = client.get_certificate(name)
                if full and full.cer:
                    crt = x509.load_der_x509_certificate(bytes(full.cer))
                    subject = crt.subject.rfc4514_string()
                    issuer = crt.issuer.rfc4514_string()
                    subject_cn = _cn(crt.subject)
                    issuer_cn = _cn(crt.issuer)
                    not_after = getattr(crt, "not_valid_after_utc", None) or crt.not_valid_after
            except Exception:
                pass  # fall back to list properties (name + expires_on)
            if not_after is None:
                continue
            out.append(RawCert(
                name=name, not_after=ensure_utc(not_after),
                subject_cn=subject_cn, subject=subject,
                issuer_cn=issuer_cn, issuer=issuer,
                entity_url="https://portal.azure.com/",
            ))
        return out
