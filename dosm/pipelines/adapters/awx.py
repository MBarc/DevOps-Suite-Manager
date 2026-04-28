"""Ansible AWX / Ansible Tower adapter.

Launches a job template via POST /api/v2/job_templates/{id}/launch/ and
polls /api/v2/jobs/{id}/ for status.

Auth: bearer token (Users → Tokens → Add in the AWX UI).
Config keys: base_url, job_template_id, inventory_id, limit, verify_ssl.
Inputs: passed as extra_vars to the job template.
"""
from __future__ import annotations

from datetime import datetime

import httpx

from dosm.pipelines.adapters.base import (
    FieldSpec,
    PipelineAdapter,
    PipelineProviderError,
    PipelineUnreachable,
    PollResult,
    TriggerResult,
)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _map_status(status: str | None) -> str:
    return {
        "pending": "queued",
        "waiting": "queued",
        "running": "running",
        "successful": "success",
        "failed": "failed",
        "error": "failed",
        "canceled": "cancelled",
    }.get(status or "", "unknown")


class AWXAdapter(PipelineAdapter):
    provider = "awx"
    display_name = "Ansible AWX"
    credential_hint = "AWX bearer token — Users → Tokens → Add in the AWX UI."

    @classmethod
    def field_schema(cls) -> list[FieldSpec]:
        return [
            FieldSpec("awx_base_url", "base_url", "Base URL",
                      "https://awx.example.com"),
            FieldSpec("awx_job_template_id", "job_template_id", "Job template ID",
                      "5", "Numeric id shown in the job template URL."),
            FieldSpec("awx_inventory_id", "inventory_id", "Inventory ID (optional)",
                      "", "Override the template's default inventory."),
            FieldSpec("awx_limit", "limit", "Limit (optional)",
                      "", "Host pattern to restrict execution (e.g. webservers)."),
            FieldSpec("awx_verify_ssl", "verify_ssl", "Verify SSL",
                      "", "Set to 'false' to skip cert verification for self-signed certs.",
                      "true"),
        ]

    def target_summary(self, config: dict) -> str:
        base = config.get("base_url", "?")
        tid = config.get("job_template_id", "?")
        return f"{base} · template #{tid}"

    def validate_config(self, config: dict) -> dict:
        for key in ("base_url", "job_template_id"):
            if not config.get(key):
                raise PipelineProviderError(f"awx config missing {key!r}")
        out = dict(config)
        out["base_url"] = config["base_url"].rstrip("/")
        out["inventory_id"] = config.get("inventory_id") or None
        out["limit"] = config.get("limit") or None
        raw_ssl = (config.get("verify_ssl") or "true").strip().lower()
        out["verify_ssl"] = raw_ssl not in ("false", "0", "no")
        return out

    async def trigger(
        self, *, config: dict, secret: str | None, inputs: dict
    ) -> TriggerResult:
        if not secret:
            raise PipelineProviderError("awx requires a bearer token credential")
        cfg = self.validate_config(config)
        base = cfg["base_url"]

        body: dict = {}
        if inputs:
            body["extra_vars"] = inputs
        if cfg["inventory_id"]:
            try:
                body["inventory"] = int(cfg["inventory_id"])
            except (TypeError, ValueError):
                body["inventory"] = cfg["inventory_id"]
        if cfg["limit"]:
            body["limit"] = cfg["limit"]

        async with httpx.AsyncClient(timeout=20.0, verify=cfg["verify_ssl"]) as c:
            try:
                r = await c.post(
                    f"{base}/api/v2/job_templates/{cfg['job_template_id']}/launch/",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "Content-Type": "application/json",
                    },
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                raise PipelineUnreachable(f"AWX unreachable: {e}") from e

        if r.status_code == 401:
            raise PipelineProviderError("AWX rejected the token (401)")
        if r.status_code == 404:
            raise PipelineProviderError(
                f"AWX job template {cfg['job_template_id']!r} not found"
            )
        if r.status_code not in (200, 201):
            raise PipelineProviderError(
                f"AWX launch failed: {r.status_code} {r.text[:200]}"
            )

        data = r.json()
        job_id = str(data.get("job") or data.get("id") or "")
        html_url = f"{base}/#/jobs/playbook/{job_id}/output" if job_id else None
        return TriggerResult(
            external_id=job_id or None,
            status=_map_status(data.get("status")),
            html_url=html_url,
            raw=data,
        )

    async def poll(
        self, *, config: dict, secret: str | None, external_id: str | None
    ) -> PollResult:
        if not secret:
            raise PipelineProviderError("awx poll requires a token")
        if not external_id:
            return PollResult(
                status="queued", started_at=None, completed_at=None, html_url=None, raw={}
            )
        cfg = self.validate_config(config)
        base = cfg["base_url"]
        async with httpx.AsyncClient(timeout=10.0, verify=cfg["verify_ssl"]) as c:
            try:
                r = await c.get(
                    f"{base}/api/v2/jobs/{external_id}/",
                    headers={"Authorization": f"Bearer {secret}"},
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                raise PipelineUnreachable(f"AWX unreachable: {e}") from e
        if r.status_code == 404:
            return PollResult(
                status="unknown", started_at=None, completed_at=None, html_url=None, raw={}
            )
        if r.status_code != 200:
            raise PipelineProviderError(
                f"AWX poll failed: {r.status_code} {r.text[:200]}"
            )
        data = r.json()
        status = _map_status(data.get("status"))
        terminal = status in ("success", "failed", "cancelled")
        return PollResult(
            status=status,
            started_at=_parse_iso(data.get("started")),
            completed_at=_parse_iso(data.get("finished")) if terminal else None,
            html_url=f"{base}/#/jobs/playbook/{external_id}/output",
            raw=data,
        )
