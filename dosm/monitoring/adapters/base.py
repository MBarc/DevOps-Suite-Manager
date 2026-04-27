from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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


class MonitoringAdapter(ABC):
    def __init__(self, source_id: int, source_name: str) -> None:
        self.source_id = source_id
        self.source_name = source_name

    @abstractmethod
    async def check_host(self, hostname: str) -> HostCheckResult: ...
