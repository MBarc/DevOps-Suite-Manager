from __future__ import annotations

import httpx

from dosm.monitoring.adapters.base import HostCheckResult, MonitoringAdapter


class DynatraceAdapter(MonitoringAdapter):
    def __init__(self, source_id: int, source_name: str, base_url: str, token: str) -> None:
        super().__init__(source_id, source_name)
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def check_host(self, hostname: str) -> HostCheckResult:
        url = f"{self.base_url}/api/v2/entities"
        params = {
            "entitySelector": f'type("HOST"),entityName.equals("{hostname}")',
            "fields": "entityId,displayName",
        }
        headers = {"Authorization": f"Api-Token {self.token}"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            entities = data.get("entities", [])
            if not entities:
                return HostCheckResult(
                    source_id=self.source_id, source_name=self.source_name,
                    tool="dynatrace", found=False,
                )
            e = entities[0]
            eid = e.get("entityId", "")
            return HostCheckResult(
                source_id=self.source_id, source_name=self.source_name,
                tool="dynatrace", found=True,
                entity_id=eid,
                entity_name=e.get("displayName", hostname),
                entity_url=f"{self.base_url}/#entity;id={eid}",
                extra={"total_count": data.get("totalCount", 1)},
            )
        except Exception as exc:
            return HostCheckResult(
                source_id=self.source_id, source_name=self.source_name,
                tool="dynatrace", found=False, error=str(exc),
            )
