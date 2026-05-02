"""GitHub Actions adapter.

Triggers workflows via POST /repos/{owner}/{repo}/actions/workflows/{file}/dispatches
and resolves the resulting run by diffing the workflow's run list before/after
dispatch — the dispatch endpoint returns 204 with no body so we have no other
way to learn the run id.

Auth: a Personal Access Token (or fine-grained PAT) with `repo` + `workflow`
scopes. The token lives in the DOSM secrets backend keyed by the credential's
secret_ref.
"""
from __future__ import annotations

import asyncio
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
from dosm.pipelines.inputs import coerce_for_github


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # GitHub returns "2026-04-26T13:45:00Z"
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _map_status(status: str | None, conclusion: str | None) -> str:
    if status == "completed":
        return {
            "success": "success",
            "failure": "failed",
            "cancelled": "cancelled",
            "skipped": "skipped",
            "timed_out": "failed",
            "neutral": "success",
            "action_required": "running",
            "stale": "unknown",
        }.get(conclusion or "", "unknown")
    if status in ("in_progress", "waiting", "pending"):
        return "running"
    if status in ("queued", "requested"):
        return "queued"
    return "unknown"


class GitHubActionsAdapter(PipelineAdapter):
    provider = "github_actions"
    display_name = "GitHub Actions"
    credential_hint = "Personal Access Token with <code>repo</code> + <code>workflow</code> scopes."

    DEFAULT_API_BASE = "https://api.github.com"
    POST_DISPATCH_POLL_TRIES = 8
    POST_DISPATCH_POLL_DELAY = 0.5

    @classmethod
    def field_schema(cls) -> list[FieldSpec]:
        return [
            FieldSpec("gh_owner", "owner", "Owner", "my-org"),
            FieldSpec("gh_repo", "repo", "Repo", "my-repo"),
            FieldSpec("gh_workflow", "workflow", "Workflow file", "deploy.yml",
                      "Filename or numeric workflow id."),
            FieldSpec("gh_ref", "ref", "Ref (branch/tag/SHA)", "main",
                      "", "main"),
            FieldSpec("gh_api_base", "api_base", "API base (optional)",
                      "https://api.github.com",
                      "Override for GitHub Enterprise Server."),
        ]

    def target_summary(self, config: dict) -> str:
        owner = config.get("owner", "?")
        repo = config.get("repo", "?")
        wf = config.get("workflow", "?")
        return f"{owner}/{repo} · {wf}"

    def validate_config(self, config: dict) -> dict:
        for key in ("owner", "repo", "workflow"):
            if not config.get(key):
                raise PipelineProviderError(f"github_actions config missing {key!r}")
        out = dict(config)
        out["ref"] = config.get("ref") or "main"
        out["api_base"] = config.get("api_base") or self.DEFAULT_API_BASE
        return out

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def trigger(
        self, *, config: dict, secret: str | None, inputs: dict
    ) -> TriggerResult:
        if not secret:
            raise PipelineProviderError("github_actions requires a token (credential)")
        cfg = self.validate_config(config)
        owner, repo, workflow = cfg["owner"], cfg["repo"], cfg["workflow"]
        ref = cfg["ref"]
        base = cfg["api_base"].rstrip("/")
        runs_url = f"{base}/repos/{owner}/{repo}/actions/workflows/{workflow}/runs"
        dispatch_url = f"{base}/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches"

        async with httpx.AsyncClient(timeout=15.0) as c:
            try:
                # Capture the latest run id BEFORE dispatch so we can spot ours after.
                pre = await c.get(runs_url, params={"per_page": 1}, headers=self._headers(secret))
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                raise PipelineUnreachable(f"GitHub unreachable: {e}") from e
            if pre.status_code == 401:
                raise PipelineProviderError("GitHub rejected the token (401)")
            if pre.status_code == 404:
                raise PipelineProviderError(
                    f"GitHub workflow not found: {owner}/{repo} {workflow}"
                )
            pre.raise_for_status()
            previous_id = None
            previous = pre.json().get("workflow_runs") or []
            if previous:
                previous_id = previous[0].get("id")

            try:
                # GitHub's workflow_dispatch payload requires string values
                # for every input (booleans/numbers go on the wire as their
                # string repr). coerce_for_github handles that uniformly so
                # typed inputs from the run form Just Work.
                disp = await c.post(
                    dispatch_url,
                    json={"ref": ref, "inputs": coerce_for_github(inputs or {})},
                    headers=self._headers(secret),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                raise PipelineUnreachable(f"GitHub unreachable: {e}") from e
            if disp.status_code == 422:
                raise PipelineProviderError(
                    f"GitHub rejected dispatch (422): {disp.text[:200]}"
                )
            if disp.status_code not in (200, 201, 204):
                raise PipelineProviderError(
                    f"GitHub dispatch failed: {disp.status_code} {disp.text[:200]}"
                )

            # Poll briefly to surface the new run id.
            for _ in range(self.POST_DISPATCH_POLL_TRIES):
                await asyncio.sleep(self.POST_DISPATCH_POLL_DELAY)
                r = await c.get(runs_url, params={"per_page": 1}, headers=self._headers(secret))
                if r.status_code != 200:
                    continue
                runs = r.json().get("workflow_runs") or []
                if not runs:
                    continue
                top = runs[0]
                if previous_id is None or top.get("id") != previous_id:
                    return TriggerResult(
                        external_id=str(top["id"]),
                        status=_map_status(top.get("status"), top.get("conclusion")),
                        html_url=top.get("html_url"),
                        raw=top,
                    )

        return TriggerResult(
            external_id=None,
            status="queued",
            html_url=None,
            raw={"note": "dispatch accepted; run id not yet visible — refresh later"},
        )

    async def poll(
        self, *, config: dict, secret: str | None, external_id: str | None
    ) -> PollResult:
        if not secret:
            raise PipelineProviderError("github_actions poll requires a token")
        if not external_id:
            return PollResult(status="queued", started_at=None, completed_at=None, html_url=None, raw={})
        cfg = self.validate_config(config)
        base = cfg["api_base"].rstrip("/")
        url = f"{base}/repos/{cfg['owner']}/{cfg['repo']}/actions/runs/{external_id}"
        async with httpx.AsyncClient(timeout=10.0) as c:
            try:
                r = await c.get(url, headers=self._headers(secret))
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                raise PipelineUnreachable(f"GitHub unreachable: {e}") from e
        if r.status_code == 404:
            return PollResult(status="unknown", started_at=None, completed_at=None, html_url=None, raw={})
        if r.status_code != 200:
            raise PipelineProviderError(f"GitHub poll failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        return PollResult(
            status=_map_status(data.get("status"), data.get("conclusion")),
            started_at=_parse_iso(data.get("run_started_at") or data.get("created_at")),
            completed_at=_parse_iso(data.get("updated_at")) if data.get("status") == "completed" else None,
            html_url=data.get("html_url"),
            raw=data,
        )
