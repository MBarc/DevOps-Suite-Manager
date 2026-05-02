"""Azure DevOps Pipelines adapter.

Triggers runs via POST /{org}/{project}/_apis/pipelines/{id}/runs and polls
the same endpoint for status.  Auth is a PAT encoded as HTTP Basic.

Config keys: organization, project, pipeline_id, branch, api_base.
Inputs: plain keys become templateParameters; keys prefixed with "var." become
runtime variables (the prefix is stripped).
"""
from __future__ import annotations

import base64
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
from dosm.pipelines.inputs import split_azure_devops_inputs


def _b64_pat(token: str) -> str:
    return base64.b64encode(f":{token}".encode()).decode()


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Basic {_b64_pat(token)}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _map_status(state: str | None, result: str | None) -> str:
    if state == "completed":
        return {
            "succeeded": "success",
            "failed": "failed",
            "canceled": "cancelled",
        }.get(result or "", "unknown")
    if state in ("inProgress", "canceling"):
        return "running"
    return "queued"


class AzureDevOpsAdapter(PipelineAdapter):
    provider = "azure_devops"
    display_name = "Azure DevOps"
    credential_hint = "Personal Access Token with <em>Build (Read &amp; Execute)</em> scope."

    DEFAULT_API_BASE = "https://dev.azure.com"
    API_VERSION = "7.0"

    @classmethod
    def field_schema(cls) -> list[FieldSpec]:
        return [
            FieldSpec("ado_org", "organization", "Organization", "my-org"),
            FieldSpec("ado_project", "project", "Project", "MyProject"),
            FieldSpec("ado_pipeline_id", "pipeline_id", "Pipeline ID", "42",
                      "Numeric id in the Pipelines list URL."),
            FieldSpec("ado_branch", "branch", "Branch", "refs/heads/main",
                      "Ref to build (e.g. refs/heads/main).", "refs/heads/main"),
            FieldSpec("ado_api_base", "api_base", "API base (optional)",
                      "https://dev.azure.com",
                      "Override for Azure DevOps Server (on-prem)."),
        ]

    def target_summary(self, config: dict) -> str:
        org = config.get("organization", "?")
        proj = config.get("project", "?")
        pid = config.get("pipeline_id", "?")
        return f"{org}/{proj} · pipeline #{pid}"

    def validate_config(self, config: dict) -> dict:
        for key in ("organization", "project", "pipeline_id"):
            if not config.get(key):
                raise PipelineProviderError(f"azure_devops config missing {key!r}")
        out = dict(config)
        out["branch"] = config.get("branch") or "refs/heads/main"
        out["api_base"] = (config.get("api_base") or self.DEFAULT_API_BASE).rstrip("/")
        return out

    def _runs_url(self, cfg: dict, run_id: str | int = "") -> str:
        base = cfg["api_base"]
        org, proj, pid = cfg["organization"], cfg["project"], cfg["pipeline_id"]
        url = f"{base}/{org}/{proj}/_apis/pipelines/{pid}/runs"
        if run_id:
            url += f"/{run_id}"
        return f"{url}?api-version={self.API_VERSION}"

    async def trigger(
        self, *, config: dict, secret: str | None, inputs: dict
    ) -> TriggerResult:
        if not secret:
            raise PipelineProviderError("azure_devops requires a PAT credential")
        cfg = self.validate_config(config)

        template_params, variables = split_azure_devops_inputs(inputs or {})

        body: dict = {
            "resources": {
                "repositories": {"self": {"refName": cfg["branch"]}}
            },
        }
        if template_params:
            body["templateParameters"] = template_params
        if variables:
            body["variables"] = {name: {"value": val} for name, val in variables.items()}

        async with httpx.AsyncClient(timeout=20.0) as c:
            try:
                r = await c.post(
                    self._runs_url(cfg),
                    json=body,
                    headers=_headers(secret),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                raise PipelineUnreachable(f"Azure DevOps unreachable: {e}") from e

        if r.status_code == 401:
            raise PipelineProviderError("Azure DevOps rejected the PAT (401)")
        if r.status_code == 404:
            raise PipelineProviderError(
                f"Azure DevOps pipeline not found: "
                f"{cfg['organization']}/{cfg['project']} #{cfg['pipeline_id']}"
            )
        if r.status_code not in (200, 201):
            raise PipelineProviderError(
                f"Azure DevOps trigger failed: {r.status_code} {r.text[:200]}"
            )

        data = r.json()
        run_id = str(data.get("id", ""))
        html_url = (
            data.get("_links", {}).get("web", {}).get("href")
            or (
                f"{cfg['api_base']}/{cfg['organization']}/{cfg['project']}"
                f"/_build/results?buildId={run_id}"
                if run_id else None
            )
        )
        return TriggerResult(
            external_id=run_id or None,
            status=_map_status(data.get("state"), data.get("result")),
            html_url=html_url,
            raw=data,
        )

    async def poll(
        self, *, config: dict, secret: str | None, external_id: str | None
    ) -> PollResult:
        if not secret:
            raise PipelineProviderError("azure_devops poll requires a PAT")
        if not external_id:
            return PollResult(
                status="queued", started_at=None, completed_at=None, html_url=None, raw={}
            )
        cfg = self.validate_config(config)
        async with httpx.AsyncClient(timeout=10.0) as c:
            try:
                r = await c.get(
                    self._runs_url(cfg, external_id),
                    headers=_headers(secret),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                raise PipelineUnreachable(f"Azure DevOps unreachable: {e}") from e
        if r.status_code == 404:
            return PollResult(
                status="unknown", started_at=None, completed_at=None, html_url=None, raw={}
            )
        if r.status_code != 200:
            raise PipelineProviderError(
                f"Azure DevOps poll failed: {r.status_code} {r.text[:200]}"
            )
        data = r.json()
        html_url = data.get("_links", {}).get("web", {}).get("href")
        state = data.get("state")
        return PollResult(
            status=_map_status(state, data.get("result")),
            started_at=_parse_iso(data.get("createdDate")),
            completed_at=_parse_iso(data.get("finishedDate")) if state == "completed" else None,
            html_url=html_url,
            raw=data,
        )
