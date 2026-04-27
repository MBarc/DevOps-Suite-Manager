from __future__ import annotations

import httpx

from dosm.monitoring.adapters.base import HostCheckResult, MonitoringAdapter


class DatadogAdapter(MonitoringAdapter):
    def __init__(
        self, source_id: int, source_name: str, site: str, api_key: str, app_key: str
    ) -> None:
        super().__init__(source_id, source_name)
        self.site = site
        self.api_key = api_key
        self.app_key = app_key

    async def check_host(self, hostname: str) -> HostCheckResult:
        url = f"https://api.{self.site}/api/v1/hosts"
        params = {"filter": f"hostname:{hostname}"}
        headers = {
            "DD-API-KEY": self.api_key,
            "DD-APPLICATION-KEY": self.app_key,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            host_list = data.get("host_list", [])
            if not host_list:
                return HostCheckResult(
                    source_id=self.source_id, source_name=self.source_name,
                    tool="datadog", found=False,
                )
            h = host_list[0]
            host_name = h.get("host_name", hostname)
            return HostCheckResult(
                source_id=self.source_id, source_name=self.source_name,
                tool="datadog", found=True,
                entity_id=str(h.get("id", "")),
                entity_name=host_name,
                entity_url=f"https://app.{self.site}/infrastructure?host={host_name}",
                extra={
                    "up": h.get("up", True),
                    "last_reported": h.get("last_reported_time"),
                },
            )
        except Exception as exc:
            return HostCheckResult(
                source_id=self.source_id, source_name=self.source_name,
                tool="datadog", found=False, error=str(exc),
            )
