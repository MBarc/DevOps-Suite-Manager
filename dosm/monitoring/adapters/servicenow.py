from __future__ import annotations

import httpx

from dosm.monitoring.adapters.base import HostCheckResult, MonitoringAdapter


class ServiceNowAdapter(MonitoringAdapter):
    def __init__(
        self, source_id: int, source_name: str, base_url: str, username: str, password: str
    ) -> None:
        super().__init__(source_id, source_name)
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

    async def check_host(self, hostname: str) -> HostCheckResult:
        url = f"{self.base_url}/api/now/table/cmdb_ci_server"
        params = {
            "sysparm_query": f"name={hostname}",
            "sysparm_limit": "5",
            "sysparm_fields": "sys_id,name,fqdn,operational_status,sys_class_name",
        }
        try:
            async with httpx.AsyncClient(
                timeout=10, auth=(self.username, self.password)
            ) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            results = data.get("result", [])
            if not results:
                return HostCheckResult(
                    source_id=self.source_id, source_name=self.source_name,
                    tool="servicenow", found=False,
                )
            ci = results[0]
            sys_id = ci.get("sys_id", "")
            return HostCheckResult(
                source_id=self.source_id, source_name=self.source_name,
                tool="servicenow", found=True,
                entity_id=sys_id,
                entity_name=ci.get("name", hostname),
                entity_url=f"{self.base_url}/nav_to.do?uri=cmdb_ci_server.do?sys_id={sys_id}",
                extra={
                    "operational_status": ci.get("operational_status"),
                    "class": ci.get("sys_class_name"),
                    "fqdn": ci.get("fqdn"),
                },
            )
        except Exception as exc:
            return HostCheckResult(
                source_id=self.source_id, source_name=self.source_name,
                tool="servicenow", found=False, error=str(exc),
            )
