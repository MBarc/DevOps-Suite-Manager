"""Terraform Cloud / Terraform Enterprise adapter.

Creates a run via POST /api/v2/runs (JSON:API format) and polls
GET /api/v2/runs/{id} for status.

Auth: bearer token (TFC/TFE → User Settings → Tokens, or a team token).
Config keys: base_url, workspace_id, auto_apply, is_destroy, message.
Note: TFC variables live on the workspace, not on the run — trigger-time
inputs are not forwarded. Configure workspace variables in TFC itself.
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

_RUNNING_STATUSES = frozenset({
    "pending", "fetching", "fetching_completed", "queuing",
    "plan_queued", "planning", "planned", "cost_estimating",
    "cost_estimated", "policy_checking", "policy_checked",
    "apply_queued", "applying", "post_plan_running",
    "post_plan_completed", "confirmed",
})
_TERMINAL_OK = frozenset({"applied"})
_TERMINAL_FAIL = frozenset({"errored"})
_TERMINAL_CANCEL = frozenset({"discarded", "canceled", "force_canceled"})


def _map_status(status: str | None) -> str:
    if status in _TERMINAL_OK:
        return "success"
    if status in _TERMINAL_FAIL:
        return "failed"
    if status in _TERMINAL_CANCEL:
        return "cancelled"
    if status in _RUNNING_STATUSES:
        return "running"
    return "unknown"


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _coerce_bool(val: str | None, default: bool) -> bool:
    if not val:
        return default
    return val.strip().lower() in ("true", "1", "yes")


class TerraformCloudAdapter(PipelineAdapter):
    provider = "terraform_cloud"
    display_name = "Terraform Cloud"
    credential_hint = (
        "API token — TFC/TFE → User Settings → Tokens, or a team/org token."
    )

    DEFAULT_BASE = "https://app.terraform.io"
    _CONTENT_TYPE = "application/vnd.api+json"

    @classmethod
    def field_schema(cls) -> list[FieldSpec]:
        return [
            FieldSpec("tfc_base_url", "base_url", "Base URL",
                      "https://app.terraform.io",
                      "Override for Terraform Enterprise (on-prem).",
                      "https://app.terraform.io"),
            FieldSpec("tfc_workspace_id", "workspace_id", "Workspace ID",
                      "ws-…",
                      "Found in workspace settings — starts with ws-."),
            FieldSpec("tfc_auto_apply", "auto_apply", "Auto-apply",
                      "", "Set to 'true' to apply automatically after plan.", "false"),
            FieldSpec("tfc_is_destroy", "is_destroy", "Destroy run",
                      "", "Set to 'true' to trigger a destroy plan.", "false"),
            FieldSpec("tfc_message", "message", "Run message (optional)",
                      "", "", "Triggered from DOSM"),
        ]

    def target_summary(self, config: dict) -> str:
        return config.get("workspace_id", "?")

    def validate_config(self, config: dict) -> dict:
        if not config.get("workspace_id"):
            raise PipelineProviderError("terraform_cloud config missing 'workspace_id'")
        out = dict(config)
        out["base_url"] = (config.get("base_url") or self.DEFAULT_BASE).rstrip("/")
        out["auto_apply"] = _coerce_bool(config.get("auto_apply"), False)
        out["is_destroy"] = _coerce_bool(config.get("is_destroy"), False)
        out["message"] = config.get("message") or "Triggered from DOSM"
        return out

    def _api_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": self._CONTENT_TYPE,
        }

    async def trigger(
        self, *, config: dict, secret: str | None, inputs: dict
    ) -> TriggerResult:
        if not secret:
            raise PipelineProviderError("terraform_cloud requires an API token credential")
        cfg = self.validate_config(config)
        base = cfg["base_url"]

        body = {
            "data": {
                "type": "runs",
                "attributes": {
                    "message": cfg["message"],
                    "is-destroy": cfg["is_destroy"],
                    "auto-apply": cfg["auto_apply"],
                },
                "relationships": {
                    "workspace": {
                        "data": {"type": "workspaces", "id": cfg["workspace_id"]}
                    }
                },
            }
        }

        async with httpx.AsyncClient(timeout=20.0) as c:
            try:
                r = await c.post(
                    f"{base}/api/v2/runs",
                    json=body,
                    headers=self._api_headers(secret),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                raise PipelineUnreachable(f"Terraform Cloud unreachable: {e}") from e

        if r.status_code == 401:
            raise PipelineProviderError("Terraform Cloud rejected the token (401)")
        if r.status_code == 404:
            raise PipelineProviderError(
                f"Terraform Cloud workspace not found: {cfg['workspace_id']!r}"
            )
        if r.status_code not in (200, 201):
            raise PipelineProviderError(
                f"Terraform Cloud run creation failed: {r.status_code} {r.text[:200]}"
            )

        data = r.json()
        run_data = data.get("data", {})
        run_id = run_data.get("id", "")
        attrs = run_data.get("attributes", {})
        html_url = run_data.get("links", {}).get("html")
        return TriggerResult(
            external_id=run_id or None,
            status=_map_status(attrs.get("status")),
            html_url=html_url,
            raw=data,
        )

    async def poll(
        self, *, config: dict, secret: str | None, external_id: str | None
    ) -> PollResult:
        if not secret:
            raise PipelineProviderError("terraform_cloud poll requires a token")
        if not external_id:
            return PollResult(
                status="queued", started_at=None, completed_at=None, html_url=None, raw={}
            )
        cfg = self.validate_config(config)
        base = cfg["base_url"]
        async with httpx.AsyncClient(timeout=10.0) as c:
            try:
                r = await c.get(
                    f"{base}/api/v2/runs/{external_id}",
                    headers=self._api_headers(secret),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                raise PipelineUnreachable(f"Terraform Cloud unreachable: {e}") from e
        if r.status_code == 404:
            return PollResult(
                status="unknown", started_at=None, completed_at=None, html_url=None, raw={}
            )
        if r.status_code != 200:
            raise PipelineProviderError(
                f"Terraform Cloud poll failed: {r.status_code} {r.text[:200]}"
            )
        data = r.json()
        run_data = data.get("data", {})
        attrs = run_data.get("attributes", {})
        status = _map_status(attrs.get("status"))
        html_url = run_data.get("links", {}).get("html")
        timestamps = attrs.get("status-timestamps", {})
        started = _parse_iso(
            timestamps.get("planning-at") or timestamps.get("plan-queued-at")
        )
        terminal = status in ("success", "failed", "cancelled")
        completed = _parse_iso(
            timestamps.get("applied-at")
            or timestamps.get("errored-at")
            or timestamps.get("canceled-at")
            or timestamps.get("discarded-at")
        ) if terminal else None
        return PollResult(
            status=status,
            started_at=started,
            completed_at=completed,
            html_url=html_url,
            raw=data,
        )
