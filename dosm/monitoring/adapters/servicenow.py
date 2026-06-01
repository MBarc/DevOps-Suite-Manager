from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

import httpx

from dosm.monitoring.adapters.base import CertInfo, HostCheckResult, MonitoringAdapter, cert_status


def _extract_cn(dn: str) -> str:
    m = re.search(r"(?:^|,)\s*CN=([^,]+)", dn, re.IGNORECASE)
    return m.group(1).strip() if m else ""

_THRESHOLDS_NOTE = "Threshold values are not available via the ServiceNow API"


class ServiceNowAdapter(MonitoringAdapter):
    def __init__(
        self, source_id: int, source_name: str, base_url: str, username: str, password: str
    ) -> None:
        super().__init__(source_id, source_name)
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

    async def _fetch_cmdb(self, client: httpx.AsyncClient, hostname: str) -> list[dict]:
        resp = await client.get(
            f"{self.base_url}/api/now/table/cmdb_ci_server",
            params={
                "sysparm_query": f"name={hostname}",
                "sysparm_limit": "5",
                "sysparm_fields": (
                    "sys_id,name,fqdn,operational_status,sys_class_name,"
                    "discovery_source,last_discovered,monitor"
                ),
                "sysparm_display_value": "true",
            },
        )
        resp.raise_for_status()
        return resp.json().get("result", [])

    async def _fetch_relationships(self, client: httpx.AsyncClient, sys_id: str) -> list[dict]:
        try:
            resp = await client.get(
                f"{self.base_url}/api/now/table/cmdb_rel_ci",
                params={
                    "sysparm_query": f"child={sys_id}^type.nameLIKEMonitor",
                    "sysparm_limit": "10",
                    "sysparm_fields": "parent,type",
                    "sysparm_display_value": "true",
                },
            )
            resp.raise_for_status()
            return [
                {
                    "name": r.get("parent", ""),
                    "rel_type": r.get("type", ""),
                }
                for r in resp.json().get("result", [])
                if r.get("parent")
            ]
        except Exception:
            return []

    async def _fetch_metrics(self, client: httpx.AsyncClient, sys_id: str) -> dict:
        try:
            resp = await client.get(
                f"{self.base_url}/api/now/table/metric_instance",
                params={
                    "sysparm_query": f"ci={sys_id}",
                    "sysparm_limit": "50",
                    "sysparm_fields": "definition",
                    "sysparm_display_value": "true",
                },
            )
            if resp.status_code in (403, 404):
                return {"status": "unavailable", "items": []}
            resp.raise_for_status()
            items = [
                r.get("definition", "")
                for r in resp.json().get("result", [])
                if r.get("definition")
            ]
            return {"status": "available" if items else "empty", "items": items}
        except Exception:
            return {"status": "unavailable", "items": []}

    async def check_host(self, hostname: str) -> HostCheckResult:
        async with httpx.AsyncClient(
            timeout=10, auth=(self.username, self.password)
        ) as client:
            try:
                ci_list = await self._fetch_cmdb(client, hostname)
            except Exception as exc:
                return HostCheckResult(
                    source_id=self.source_id, source_name=self.source_name,
                    tool="servicenow", found=False, error=str(exc),
                )

            if not ci_list:
                return HostCheckResult(
                    source_id=self.source_id, source_name=self.source_name,
                    tool="servicenow", found=False,
                )

            primary = ci_list[0]
            sys_id = primary.get("sys_id", "")

            relationships, metrics = await asyncio.gather(
                self._fetch_relationships(client, sys_id),
                self._fetch_metrics(client, sys_id),
            )

        monitor_raw = primary.get("monitor")
        monitor_enabled = monitor_raw in (True, "true", "1")

        return HostCheckResult(
            source_id=self.source_id,
            source_name=self.source_name,
            tool="servicenow",
            found=True,
            entity_id=sys_id,
            entity_name=primary.get("name", hostname),
            entity_url=f"{self.base_url}/nav_to.do?uri=cmdb_ci_server.do?sys_id={sys_id}",
            extra={
                "operational_status": primary.get("operational_status") or None,
                "class": primary.get("sys_class_name") or None,
                "fqdn": primary.get("fqdn") or None,
                "discovery_source": primary.get("discovery_source") or None,
                "last_discovered": primary.get("last_discovered") or None,
                "monitor_enabled": monitor_enabled,
                "monitoring_relationships": relationships,
                "metric_collection_status": metrics["status"],
                "metric_collection": metrics["items"],
                "thresholds_note": _THRESHOLDS_NOTE,
                "additional_matches": [
                    {
                        "name": ci.get("name", ""),
                        "sys_id": ci.get("sys_id", ""),
                        "class": ci.get("sys_class_name", ""),
                        "fqdn": ci.get("fqdn", ""),
                        "url": (
                            f"{self.base_url}/nav_to.do"
                            f"?uri=cmdb_ci_server.do?sys_id={ci.get('sys_id', '')}"
                        ),
                    }
                    for ci in ci_list[1:]
                ],
            },
        )

    async def fetch_certificates(self, warn_days: int = 30, critical_days: int = 14) -> list[CertInfo]:
        # Queries the cmdb_ci_certificate table from ServiceNow Certificate Management.
        # Requires the Certificate Management plugin (com.snc.certificate_management) to be active.
        async with httpx.AsyncClient(timeout=15, auth=(self.username, self.password)) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/api/now/table/cmdb_ci_certificate",
                    params={
                        "sysparm_fields": "sys_id,name,subject,issuer,valid_to,valid_from,serial_number",
                        "sysparm_limit": "500",
                        "sysparm_display_value": "false",
                    },
                )
                resp.raise_for_status()
                rows = resp.json().get("result", [])
            except Exception:
                return []

        results: list[CertInfo] = []
        for row in rows:
            valid_to_str = (row.get("valid_to") or "").strip()
            if not valid_to_str:
                continue
            not_after = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    not_after = datetime.strptime(valid_to_str, fmt).replace(tzinfo=UTC)
                    break
                except ValueError:
                    continue
            if not_after is None:
                continue

            not_before = None
            valid_from_str = (row.get("valid_from") or "").strip()
            if valid_from_str:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        not_before = datetime.strptime(valid_from_str, fmt).replace(tzinfo=UTC)
                        break
                    except ValueError:
                        continue

            sys_id = row.get("sys_id", "")
            name = (row.get("name") or "").strip()
            subject = (row.get("subject") or name).strip()
            issuer = (row.get("issuer") or "").strip()
            serial = row.get("serial_number") or None

            subject_cn = name or _extract_cn(subject) or subject
            issuer_cn = _extract_cn(issuer) or issuer

            status, days = cert_status(not_after, warn_days, critical_days)
            results.append(CertInfo(
                endpoint=name or subject_cn,
                subject_cn=subject_cn,
                subject=subject,
                issuer_cn=issuer_cn,
                issuer=issuer,
                not_after=not_after,
                not_before=not_before,
                days_remaining=days,
                status=status,
                source_id=self.source_id,
                source_name=self.source_name,
                tool="servicenow",
                serial=serial,
                entity_url=(
                    f"{self.base_url}/nav_to.do?uri=cmdb_ci_certificate.do?sys_id={sys_id}"
                    if sys_id else None
                ),
            ))
        return results
