from __future__ import annotations

from datetime import UTC, datetime

import httpx

from dosm.monitoring.adapters.base import CertInfo, HostCheckResult, MonitoringAdapter, cert_status


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

    async def fetch_certificates(self, warn_days: int = 30, critical_days: int = 14) -> list[CertInfo]:
        # Requires blackbox_exporter with an ssl module probe scraping your targets.
        # The probe_ssl_earliest_cert_expiry metric gives the leaf cert expiry as a
        # Unix timestamp per instance label.
        url = f"{self.base_url}/api/v1/query"
        params = {"query": "probe_ssl_earliest_cert_expiry"}
        try:
            async with httpx.AsyncClient(
                timeout=10, auth=self._auth(), headers=self._headers()
            ) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            results: list[CertInfo] = []
            for series in data.get("data", {}).get("result", []):
                metric = series.get("metric", {})
                instance = metric.get("instance", "")
                raw_val = series.get("value", [None, None])[1]
                if not instance or raw_val is None:
                    continue
                try:
                    not_after = datetime.fromtimestamp(float(raw_val), tz=UTC)
                except (ValueError, TypeError):
                    continue
                status, days = cert_status(not_after, warn_days, critical_days)
                results.append(CertInfo(
                    endpoint=instance,
                    subject_cn=instance,
                    subject=instance,
                    issuer_cn="",
                    issuer="",
                    not_after=not_after,
                    days_remaining=days,
                    status=status,
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tool="prometheus",
                    entity_url=(
                        f"{self.base_url}/graph"
                        f"?g0.expr=probe_ssl_earliest_cert_expiry%7Binstance%3D%22{instance}%22%7D"
                    ),
                ))
            return results
        except Exception:
            return []
