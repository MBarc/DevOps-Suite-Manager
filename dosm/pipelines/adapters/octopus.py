"""Octopus Deploy adapter.

Resolves the latest release for the project (or a pinned version), then
creates a deployment via the Deployments endpoint.  The run identifier stored
in DOSM is the Octopus TaskId so polling goes to /api/{space}/tasks/{taskId}.

Auth: API key via X-Octopus-ApiKey header.
Config keys: base_url, space_id, project_id, environment_id, tenant_id,
             release_version.
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
from dosm.pipelines.inputs import coerce_for_octopus


def _headers(api_key: str) -> dict[str, str]:
    return {
        "X-Octopus-ApiKey": api_key,
        "Content-Type": "application/json",
    }


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _map_status(state: str | None) -> str:
    return {
        "Queued": "queued",
        "Executing": "running",
        "Cancelling": "running",
        "Success": "success",
        "Failed": "failed",
        "TimedOut": "failed",
        "Canceled": "cancelled",
    }.get(state or "", "unknown")


class OctopusDeployAdapter(PipelineAdapter):
    provider = "octopus_deploy"
    display_name = "Octopus Deploy"
    credential_hint = "Octopus API key - your profile to API Keys to New API Key."

    @classmethod
    def field_schema(cls) -> list[FieldSpec]:
        return [
            FieldSpec("oct_base_url", "base_url", "Base URL",
                      "https://octopus.example.com"),
            FieldSpec("oct_space_id", "space_id", "Space ID",
                      "Spaces-1", "", "Spaces-1"),
            FieldSpec("oct_project_id", "project_id", "Project ID or slug",
                      "Projects-1", "e.g. Projects-1 or your project slug."),
            FieldSpec("oct_environment_id", "environment_id", "Environment ID",
                      "Environments-1"),
            FieldSpec("oct_tenant_id", "tenant_id", "Tenant ID (optional)", "",
                      "Required only for multi-tenant deployments."),
            FieldSpec("oct_release_version", "release_version",
                      "Release version (optional)", "",
                      "Leave blank to deploy the latest release."),
        ]

    def target_summary(self, config: dict) -> str:
        proj = config.get("project_id", "?")
        env = config.get("environment_id", "?")
        return f"{proj} -> {env}"

    def validate_config(self, config: dict) -> dict:
        for key in ("base_url", "project_id", "environment_id"):
            if not config.get(key):
                raise PipelineProviderError(f"octopus_deploy config missing {key!r}")
        out = dict(config)
        out["base_url"] = config["base_url"].rstrip("/")
        out["space_id"] = config.get("space_id") or "Spaces-1"
        out["release_version"] = config.get("release_version") or None
        out["tenant_id"] = config.get("tenant_id") or None
        return out

    async def trigger(
        self, *, config: dict, secret: str | None, inputs: dict
    ) -> TriggerResult:
        if not secret:
            raise PipelineProviderError("octopus_deploy requires an API key credential")
        cfg = self.validate_config(config)
        base = cfg["base_url"]
        space = cfg["space_id"]
        hdrs = _headers(secret)

        async with httpx.AsyncClient(timeout=20.0) as c:
            if cfg["release_version"]:
                # Fetch a specific release by version number.
                try:
                    rr = await c.get(
                        f"{base}/api/{space}/projects/{cfg['project_id']}"
                        f"/releases/{cfg['release_version']}",
                        headers=hdrs,
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                    raise PipelineUnreachable(f"Octopus unreachable: {e}") from e
                if rr.status_code == 401:
                    raise PipelineProviderError("Octopus rejected the API key (401)")
                if rr.status_code != 200:
                    raise PipelineProviderError(
                        f"Octopus release {cfg['release_version']!r} not found "
                        f"for project {cfg['project_id']!r}"
                    )
                release_id = rr.json().get("Id")
            else:
                # Fetch the latest release.
                try:
                    rr = await c.get(
                        f"{base}/api/{space}/projects/{cfg['project_id']}/releases?take=1",
                        headers=hdrs,
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                    raise PipelineUnreachable(f"Octopus unreachable: {e}") from e
                if rr.status_code == 401:
                    raise PipelineProviderError("Octopus rejected the API key (401)")
                if rr.status_code == 404:
                    raise PipelineProviderError(
                        f"Octopus project not found: {cfg['project_id']!r}"
                    )
                if rr.status_code != 200:
                    raise PipelineProviderError(
                        f"Octopus releases fetch failed: {rr.status_code} {rr.text[:200]}"
                    )
                items = rr.json().get("Items") or []
                if not items:
                    raise PipelineProviderError(
                        f"No releases found for Octopus project {cfg['project_id']!r}"
                    )
                release_id = items[0]["Id"]

            body: dict = {
                "ReleaseId": release_id,
                "EnvironmentId": cfg["environment_id"],
            }
            if cfg["tenant_id"]:
                body["TenantId"] = cfg["tenant_id"]
            if inputs:
                body["FormValues"] = coerce_for_octopus(inputs)

            try:
                dr = await c.post(
                    f"{base}/api/{space}/deployments",
                    json=body,
                    headers=hdrs,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                raise PipelineUnreachable(f"Octopus unreachable: {e}") from e

        if dr.status_code not in (200, 201):
            raise PipelineProviderError(
                f"Octopus deployment creation failed: {dr.status_code} {dr.text[:200]}"
            )
        data = dr.json()
        task_id = data.get("TaskId") or ""
        html_url = f"{base}/app#/{space}/tasks/{task_id}" if task_id else None
        return TriggerResult(
            external_id=task_id or None,
            status="queued",
            html_url=html_url,
            raw=data,
        )

    async def poll(
        self, *, config: dict, secret: str | None, external_id: str | None
    ) -> PollResult:
        if not secret:
            raise PipelineProviderError("octopus_deploy poll requires an API key")
        if not external_id:
            return PollResult(
                status="queued", started_at=None, completed_at=None, html_url=None, raw={}
            )
        cfg = self.validate_config(config)
        base = cfg["base_url"]
        space = cfg["space_id"]
        async with httpx.AsyncClient(timeout=10.0) as c:
            try:
                r = await c.get(
                    f"{base}/api/{space}/tasks/{external_id}",
                    headers=_headers(secret),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                raise PipelineUnreachable(f"Octopus unreachable: {e}") from e
        if r.status_code == 404:
            return PollResult(
                status="unknown", started_at=None, completed_at=None, html_url=None, raw={}
            )
        if r.status_code != 200:
            raise PipelineProviderError(
                f"Octopus poll failed: {r.status_code} {r.text[:200]}"
            )
        data = r.json()
        state = data.get("State")
        terminal = state in ("Success", "Failed", "TimedOut", "Canceled")
        return PollResult(
            status=_map_status(state),
            started_at=_parse_iso(data.get("StartTime")),
            completed_at=_parse_iso(data.get("CompletedTime")) if terminal else None,
            html_url=f"{base}/app#/{space}/tasks/{external_id}",
            raw=data,
        )
