from __future__ import annotations

from datetime import UTC, datetime

import httpx

from dosm.monitoring.adapters.base import CertInfo, HostCheckResult, MonitoringAdapter, cert_status


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

    async def fetch_certificates(self, warn_days: int = 30, critical_days: int = 14) -> list[CertInfo]:
        # Requires Dynatrace HTTP synthetic monitors with SSL certificate monitoring enabled.
        # The builtin:synthetic.http.ssl.certificate.expiryDate metric gives expiry epoch (ms)
        # per synthetic test entity.
        headers = {"Authorization": f"Api-Token {self.token}"}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self.base_url}/api/v2/metrics/query",
                    params={
                        "metricSelector": "builtin:synthetic.http.ssl.certificate.expiryDate",
                        "resolution": "1h",
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()

            entity_expiry: list[tuple[str, float]] = []
            for result in data.get("result", []):
                for series in result.get("data", []):
                    entity_id = series.get("dimensionMap", {}).get("dt.entity.synthetic_test", "")
                    values = [v for v in series.get("values", []) if v is not None]
                    if entity_id and values:
                        entity_expiry.append((entity_id, values[-1]))

            if not entity_expiry:
                return []

            # Resolve entity display names in one batch call
            entity_ids = list({eid for eid, _ in entity_expiry})
            entity_names: dict[str, str] = {}
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    eresp = await client.get(
                        f"{self.base_url}/api/v2/entities",
                        params={
                            "entitySelector": f"entityId({','.join(entity_ids[:50])})",
                            "fields": "entityId,displayName",
                        },
                        headers=headers,
                    )
                    if eresp.is_success:
                        for e in eresp.json().get("entities", []):
                            entity_names[e["entityId"]] = e.get("displayName", e["entityId"])
            except Exception:
                pass

            results: list[CertInfo] = []
            for entity_id, expiry_ms in entity_expiry:
                display = entity_names.get(entity_id, entity_id)
                not_after = datetime.fromtimestamp(expiry_ms / 1000.0, tz=UTC)
                status, days = cert_status(not_after, warn_days, critical_days)
                results.append(CertInfo(
                    endpoint=display,
                    subject_cn=display,
                    subject=display,
                    issuer_cn="",
                    issuer="",
                    not_after=not_after,
                    days_remaining=days,
                    status=status,
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tool="dynatrace",
                    entity_url=f"{self.base_url}/#entity;id={entity_id}",
                ))
            return results
        except Exception:
            return []
