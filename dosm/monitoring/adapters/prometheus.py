from __future__ import annotations

import httpx

from dosm.monitoring.adapters.base import HostCheckResult, MonitoringAdapter


class PrometheusAdapter(MonitoringAdapter):
    def __init__(
        self,
        source_id: int,
        source_name: str,
        base_url: str,
        username: str = "",
        token: str = "",
    ) -> None:
        super().__init__(source_id, source_name)
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.token = token

    def _auth(self) -> tuple[str, str] | None:
        if self.username:
            return (self.username, self.token)
        return None

    def _headers(self) -> dict[str, str]:
        if not self.username and self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    async def check_host(self, hostname: str) -> HostCheckResult:
        # Match instance label exactly or with a port suffix (:9100, etc.)
        expr = f'up{{instance=~"^{hostname}(:.+)?$"}}'
        url = f"{self.base_url}/api/v1/query"
        params = {"query": expr}
        try:
            async with httpx.AsyncClient(
                timeout=10,
                auth=self._auth(),
                headers=self._headers(),
            ) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            results = data.get("data", {}).get("result", [])
            if not results:
                return HostCheckResult(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tool="prometheus",
                    found=False,
                )

            series = results[0]
            metric = series.get("metric", {})
            instance = metric.get("instance", hostname)
            up_value = series.get("value", [None, "0"])[1]
            graph_url = f"{self.base_url}/graph?g0.expr={expr}"
            return HostCheckResult(
                source_id=self.source_id,
                source_name=self.source_name,
                tool="prometheus",
                found=True,
                entity_id=instance,
                entity_name=instance,
                entity_url=graph_url,
                extra={
                    "up": up_value == "1",
                    "job": metric.get("job", ""),
                    "series_count": len(results),
                },
            )
        except Exception as exc:
            return HostCheckResult(
                source_id=self.source_id,
                source_name=self.source_name,
                tool="prometheus",
                found=False,
                error=str(exc),
            )
