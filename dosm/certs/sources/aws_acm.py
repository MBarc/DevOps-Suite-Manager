"""AWS Certificate Manager (ACM) source. SDK extra: ``pip install 'dosm[aws]'``."""
from __future__ import annotations

from dosm.certs.sources.base import (
    CertSourceError,
    CloudCertSource,
    MissingDependencyError,
    RawCert,
    ensure_utc,
)


class AwsAcmSource(CloudCertSource):
    provider = "aws_acm"
    tool = "AWS ACM"

    def __init__(self, source_id: int, source_name: str, *, region: str,
                 credential: dict | None = None) -> None:
        super().__init__(source_id, source_name)
        self.region = region
        self.credential = credential  # dict (profile) or None (instance role)

    def _client(self):
        try:
            import boto3
        except ImportError as e:
            raise MissingDependencyError(
                "AWS SDK not installed - run: pip install 'dosm[aws]'"
            ) from e
        if not self.region:
            raise CertSourceError("AWS ACM source needs a region")
        kwargs: dict = {"region_name": self.region}
        if self.credential:
            kwargs["aws_access_key_id"] = self.credential.get("access_key_id", "")
            kwargs["aws_secret_access_key"] = self.credential.get("secret_access_key", "")
        return boto3.client("acm", **kwargs)

    def _list_raw(self) -> list[RawCert]:
        client = self._client()
        out: list[RawCert] = []
        paginator = client.get_paginator("list_certificates")
        for page in paginator.paginate():
            for summary in page.get("CertificateSummaryList", []):
                arn = summary["CertificateArn"]
                cert = client.describe_certificate(CertificateArn=arn)["Certificate"]
                not_after = cert.get("NotAfter")
                if not_after is None:
                    continue  # e.g. a pending-validation cert with no expiry yet
                domain = cert.get("DomainName") or summary.get("DomainName") or arn.rsplit("/", 1)[-1]
                issuer = cert.get("Issuer", "")
                out.append(RawCert(
                    name=domain, not_after=ensure_utc(not_after),
                    subject_cn=domain, subject=cert.get("Subject", ""),
                    issuer_cn=issuer, issuer=issuer,
                    serial=cert.get("Serial"),
                    entity_url=(
                        f"https://{self.region}.console.aws.amazon.com/acm/home"
                        f"?region={self.region}#/certificates/{arn.rsplit('/', 1)[-1]}"
                    ),
                ))
        return out
