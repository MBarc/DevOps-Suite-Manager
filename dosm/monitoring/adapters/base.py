from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class HostCheckResult:
    source_id: int
    source_name: str
    tool: str
    found: bool
    entity_id: str | None = None
    entity_name: str | None = None
    entity_url: str | None = None
    extra: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class CertInfo:
    endpoint: str
    subject_cn: str
    subject: str
    issuer_cn: str
    issuer: str
    not_after: datetime
    days_remaining: int
    status: str           # ok | warn | critical | expired
    source_id: int
    source_name: str
    tool: str
    entity_url: str | None = None
    serial: str | None = None
    not_before: datetime | None = None


def cert_status(not_after: datetime, warn_days: int, critical_days: int) -> tuple[str, int]:
    days = (not_after - datetime.now(UTC)).days
    if days < 0:
        return "expired", days
    if days <= critical_days:
        return "critical", days
    if days <= warn_days:
        return "warn", days
    return "ok", days


class MonitoringAdapter(ABC):
    def __init__(self, source_id: int, source_name: str) -> None:
        self.source_id = source_id
        self.source_name = source_name

    @abstractmethod
    async def check_host(self, hostname: str) -> HostCheckResult: ...

    async def fetch_certificates(
        self, warn_days: int = 30, critical_days: int = 14
    ) -> list[CertInfo]:
        return []
