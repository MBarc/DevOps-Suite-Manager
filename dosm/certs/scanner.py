from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.utils import CryptographyDeprecationWarning


@dataclass
class CertInfo:
    subject_cn: str
    subject: str
    issuer_cn: str
    issuer: str
    serial: str
    not_before: datetime
    not_after: datetime
    thumbprint: str
    source: str
    status: str       # ok | warn | critical | expired
    days_remaining: int


def _cn(name: x509.Name) -> str:
    for oid in (x509.NameOID.COMMON_NAME, x509.NameOID.ORGANIZATION_NAME, x509.NameOID.ORGANIZATIONAL_UNIT_NAME):
        attrs = name.get_attributes_for_oid(oid)
        if attrs:
            return str(attrs[0].value)
    return name.rfc4514_string()


def _status(not_after: datetime, warn_days: int, critical_days: int) -> tuple[str, int]:
    days = (not_after - datetime.now(UTC)).days
    if days < 0:
        return "expired", days
    if days <= critical_days:
        return "critical", days
    if days <= warn_days:
        return "warn", days
    return "ok", days


def _make_info(cert: x509.Certificate, source: str, warn_days: int, critical_days: int) -> CertInfo:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
        not_after = cert.not_valid_after_utc
        st, days = _status(not_after, warn_days, critical_days)
        return CertInfo(
            subject_cn=_cn(cert.subject),
            subject=cert.subject.rfc4514_string(),
            issuer_cn=_cn(cert.issuer),
            issuer=cert.issuer.rfc4514_string(),
            serial=format(cert.serial_number, "x"),
            not_before=cert.not_valid_before_utc,
            not_after=not_after,
            thumbprint=cert.fingerprint(hashes.SHA256()).hex(),
            source=source,
            status=st,
            days_remaining=days,
        )


def _load_der(data: bytes) -> x509.Certificate | None:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
        try:
            return x509.load_der_x509_certificate(data)
        except Exception:
            return None


def _load_pem(data: bytes) -> x509.Certificate | None:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
        try:
            return x509.load_pem_x509_certificate(data)
        except Exception:
            return None


def _scan_windows(stores: list[str], warn_days: int, critical_days: int) -> list[CertInfo]:
    import ssl
    results: list[CertInfo] = []
    for store in stores:
        try:
            for cert_bytes, enc_type, _trust in ssl.enum_certificates(store):
                if enc_type != "x509_asn":
                    continue
                cert = _load_der(cert_bytes)
                if cert is not None:
                    results.append(_make_info(cert, f"Windows:{store}", warn_days, critical_days))
        except OSError:
            pass
    return results


def _scan_paths(paths: list[str], warn_days: int, critical_days: int) -> list[CertInfo]:
    results: list[CertInfo] = []
    seen_real: set[Path] = set()
    for base in paths:
        root = Path(base)
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            real = f.resolve()
            if real in seen_real:
                continue
            seen_real.add(real)
            try:
                data = f.read_bytes()
            except OSError:
                continue
            if b"-----BEGIN CERTIFICATE-----" in data:
                cert = _load_pem(data)
            else:
                cert = _load_der(data)
            if cert is not None:
                results.append(_make_info(cert, str(f), warn_days, critical_days))
    return results


_STATUS_ORDER = {"expired": 0, "critical": 1, "warn": 2, "ok": 3}
_cache: tuple[list[CertInfo], datetime] | None = None
_CACHE_TTL = timedelta(minutes=5)


def scan_all(
    warn_days: int,
    critical_days: int,
    scan_paths: list[str],
    windows_stores: list[str],
    *,
    force: bool = False,
) -> list[CertInfo]:
    global _cache
    now = datetime.now(UTC)
    if not force and _cache is not None:
        cached, cached_at = _cache
        if now - cached_at < _CACHE_TTL:
            return cached

    if sys.platform == "win32":
        file_paths = list(scan_paths)
        results = _scan_windows(windows_stores, warn_days, critical_days)
    else:
        default = [
            p for p in ["/etc/ssl/certs", "/etc/pki/tls/certs", "/usr/local/share/ca-certificates"]
            if Path(p).exists()
        ]
        file_paths = default + list(scan_paths)
        results = []

    if file_paths:
        results.extend(_scan_paths(file_paths, warn_days, critical_days))

    # Deduplicate by thumbprint (handles symlinks / cross-store duplicates)
    seen: set[str] = set()
    deduped: list[CertInfo] = []
    for c in results:
        if c.thumbprint not in seen:
            seen.add(c.thumbprint)
            deduped.append(c)

    deduped.sort(key=lambda c: (_STATUS_ORDER.get(c.status, 9), c.not_after))
    _cache = (deduped, now)
    return deduped


def get_by_thumbprint(thumbprint: str) -> CertInfo | None:
    if _cache is None:
        return None
    certs, _ = _cache
    for c in certs:
        if c.thumbprint == thumbprint:
            return c
    return None


def peek_cached() -> tuple[list[CertInfo], datetime] | None:
    """Return the cached scan result without triggering a fresh scan.

    The dashboard reads this so it never pays the cost of a cert scan on
    page load. Callers that actually want the data should use ``scan_all``.
    """
    return _cache
