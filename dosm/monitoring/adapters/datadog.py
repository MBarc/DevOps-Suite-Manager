from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx

from dosm.monitoring.adapters.base import CertInfo, HostCheckResult, MonitoringAdapter, cert_status


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

    async def fetch_certificates(self, warn_days: int = 30, critical_days: int = 14) -> list[CertInfo]:
        # Requires the Datadog HTTP check integration (http_check) configured for your endpoints,
        # which populates the tls.days_left metric tagged with {url}.
        now_ts = int(time.time())
        headers = {
            "DD-API-KEY": self.api_key,
            "DD-APPLICATION-KEY": self.app_key,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"https://api.{self.site}/api/v1/query",
                    params={
                        "from": now_ts - 3600,
                        "to": now_ts,
                        "query": "min:tls.days_left{*}by{url}",
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()

            results: list[CertInfo] = []
            for series in data.get("series", []):
                tags = series.get("tag_set", [])
                url_tag = next((t.split(":", 1)[1] for t in tags if t.startswith("url:")), "")
                if not url_tag:
                    url_tag = series.get("display_name") or series.get("metric", "unknown")

                pointlist = series.get("pointlist", [])
                days_left = None
                for _ts, val in reversed(pointlist):
                    if val is not None:
                        days_left = float(val)
                        break
                if days_left is None:
                    continue

                not_after = datetime.now(UTC) + timedelta(days=days_left)
                status, days = cert_status(not_after, warn_days, critical_days)
                results.append(CertInfo(
                    endpoint=url_tag,
                    subject_cn=url_tag,
                    subject=url_tag,
                    issuer_cn="",
                    issuer="",
                    not_after=not_after,
                    days_remaining=days,
                    status=status,
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tool="datadog",
                    entity_url=f"https://app.{self.site}/monitors",
                ))
            return results
        except Exception:
            return []
