from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.tenancy import tenant_clause
from dosm.config import Config
from dosm.models import Pipeline, PipelinePayload, PipelineRun
from dosm.pipelines.adapters import (
    PipelineProviderError,
    PipelineUnreachable,
    PollResult,
    get_adapter,
)
from dosm.secrets import SecretNotFound, get_backend


class PipelineNotFound(LookupError):
    pass


def list_pipelines(db: Session, tid: int | None, user=None) -> list[Pipeline]:
    """Pipelines in tenant ``tid``. When ``user`` is given, also restrict to the
    pipelines that user may see (shared + their own private); when None (agent /
    CLI) no visibility restriction is applied within the tenant."""
    stmt = select(Pipeline).order_by(Pipeline.name)
    clause = tenant_clause(Pipeline, tid)
    if clause is not None:
        stmt = stmt.where(clause)
    if user is not None:
        from dosm.pipelines.access import visible_pipelines_filter
        vclause = visible_pipelines_filter(user)
        if vclause is not True:
            stmt = stmt.where(vclause)
    return list(db.execute(stmt).scalars())


def get_pipeline(db: Session, pid: int, tid: int | None) -> Pipeline | None:
    """Fetch a pipeline by id, scoped to tenant ``tid``. Returns None when it
    belongs to a different tenant so callers 404 rather than leak existence."""
    pipeline = db.get(Pipeline, pid)
    if pipeline is None:
        return None
    if tid is not None and pipeline.tenant_id != tid:
        return None
    return pipeline


def get_pipeline_by_name(db: Session, name: str, tid: int | None) -> Pipeline | None:
    stmt = select(Pipeline).where(Pipeline.name == name)
    clause = tenant_clause(Pipeline, tid)
    if clause is not None:
        stmt = stmt.where(clause)
    return db.execute(stmt).scalar_one_or_none()


def list_runs(db: Session, pipeline_id: int, limit: int = 25) -> list[PipelineRun]:
    return list(
        db.execute(
            select(PipelineRun)
            .where(PipelineRun.pipeline_id == pipeline_id)
            .order_by(PipelineRun.id.desc())
            .limit(limit)
        ).scalars()
    )


def get_run(db: Session, run_id: int) -> PipelineRun | None:
    return db.get(PipelineRun, run_id)


def create_pipeline(
    db: Session,
    *,
    tenant_id: int,
    name: str,
    provider: str,
    description: str | None,
    config: dict,
    inputs_schema: list[dict] | None,
    credential_id: int | None,
    org_unit_id: int | None = None,
    owner_id: int | None = None,
    visibility: str = "shared",
) -> Pipeline:
    adapter = get_adapter(provider)
    cfg_norm = adapter.validate_config(config)
    p = Pipeline(
        tenant_id=tenant_id,
        name=name.strip(),
        provider=provider,
        description=description or None,
        config=json.dumps(cfg_norm),
        inputs_schema=json.dumps(inputs_schema) if inputs_schema else None,
        credential_id=credential_id,
        org_unit_id=org_unit_id,
        owner_id=owner_id,
        visibility=visibility if visibility in ("shared", "private") else "shared",
    )
    db.add(p)
    db.flush()
    return p


def update_pipeline(
    db: Session,
    pipeline: Pipeline,
    *,
    name: str,
    provider: str,
    description: str | None,
    config: dict,
    inputs_schema: list[dict] | None,
    credential_id: int | None,
    org_unit_id: int | None = None,
    visibility: str | None = None,
) -> Pipeline:
    adapter = get_adapter(provider)
    cfg_norm = adapter.validate_config(config)
    pipeline.name = name.strip()
    pipeline.provider = provider
    pipeline.description = description or None
    pipeline.config = json.dumps(cfg_norm)
    pipeline.inputs_schema = json.dumps(inputs_schema) if inputs_schema else None
    pipeline.credential_id = credential_id
    pipeline.org_unit_id = org_unit_id
    if visibility is not None and visibility in ("shared", "private"):
        pipeline.visibility = visibility
    db.flush()
    return pipeline


def delete_pipeline(db: Session, pipeline: Pipeline) -> None:
    db.delete(pipeline)
    db.flush()


# ---- Payloads (saved input-value sets) -----------------------------------


class PayloadNameConflict(ValueError):
    """A payload with this display name already exists on the pipeline."""


def list_payloads(db: Session, pipeline_id: int, clause=None) -> list[PipelinePayload]:
    """Payloads for a pipeline, ordered by name. ``clause`` is an optional
    SQLAlchemy filter (from payload_access.visible_payloads_filter) limiting to
    what the requesting user may see; ``True`` / ``None`` means no restriction."""
    stmt = select(PipelinePayload).where(PipelinePayload.pipeline_id == pipeline_id)
    if clause is not None and clause is not True:
        stmt = stmt.where(clause)
    return list(db.execute(stmt.order_by(PipelinePayload.name)).scalars())


def get_payload(db: Session, payload_id: int) -> PipelinePayload | None:
    return db.get(PipelinePayload, payload_id)


def _assert_name_free(db: Session, pipeline_id: int, name: str, exclude_id: int | None = None) -> None:
    existing = db.execute(
        select(PipelinePayload).where(
            PipelinePayload.pipeline_id == pipeline_id,
            PipelinePayload.name == name,
        )
    ).scalar_one_or_none()
    if existing is not None and existing.id != exclude_id:
        raise PayloadNameConflict(f"a payload named {name!r} already exists")


def create_payload(
    db: Session,
    *,
    pipeline_id: int,
    name: str,
    values: dict,
    description: str | None = None,
    visibility: str = "shared",
    created_by_id: int | None = None,
) -> PipelinePayload:
    name = name.strip()
    if not name:
        raise ValueError("payload name is required")
    _assert_name_free(db, pipeline_id, name)
    payload = PipelinePayload(
        pipeline_id=pipeline_id,
        name=name,
        description=(description or None),
        values_json=json.dumps(values or {}),
        visibility=visibility if visibility in ("shared", "private") else "shared",
        created_by_id=created_by_id,
    )
    db.add(payload)
    db.flush()
    return payload


def update_payload(
    db: Session,
    payload: PipelinePayload,
    *,
    name: str | None = None,
    values: dict | None = None,
    description: str | None = None,
    visibility: str | None = None,
) -> PipelinePayload:
    """Redefine a payload. Any subset of fields may be passed; ``name`` covers
    the rename case (uniqueness enforced per pipeline)."""
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("payload name is required")
        _assert_name_free(db, payload.pipeline_id, name, exclude_id=payload.id)
        payload.name = name
    if values is not None:
        payload.values_json = json.dumps(values)
    if description is not None:
        payload.description = description or None
    if visibility is not None and visibility in ("shared", "private"):
        payload.visibility = visibility
    db.flush()
    return payload


def copy_payload(
    db: Session, payload: PipelinePayload, *, created_by_id: int | None = None
) -> PipelinePayload:
    """Duplicate a payload under a derived, unique name ("X (copy)", "X (copy 2)")."""
    base = f"{payload.name} (copy)"
    name = base
    n = 2
    while True:
        try:
            _assert_name_free(db, payload.pipeline_id, name)
            break
        except PayloadNameConflict:
            name = f"{base[:-1]} {n})" if base.endswith(")") else f"{base} {n}"
            n += 1
    return create_payload(
        db,
        pipeline_id=payload.pipeline_id,
        name=name,
        values=json.loads(payload.values_json or "{}"),
        description=payload.description,
        visibility=payload.visibility,
        created_by_id=created_by_id,
    )


def delete_payload(db: Session, payload: PipelinePayload) -> None:
    db.delete(payload)
    db.flush()


def _resolve_secret(cfg: Config, pipeline: Pipeline) -> str | None:
    if pipeline.credential is None:
        return None
    try:
        return get_backend(cfg).get_str(pipeline.credential.secret_ref)
    except SecretNotFound as e:
        raise PipelineProviderError(
            f"credential {pipeline.credential.name!r} secret_ref "
            f"{pipeline.credential.secret_ref!r} missing"
        ) from e


async def trigger_pipeline(
    cfg: Config,
    db: Session,
    pipeline: Pipeline,
    *,
    inputs: dict,
    user_id: int | None,
) -> PipelineRun:
    """Trigger a run, persist a PipelineRun row, and return it.

    Provider failures are recorded as PipelineRun rows with status='failed'
    and an error string so the user can see what went wrong.
    """
    adapter = get_adapter(pipeline.provider)
    config = json.loads(pipeline.config or "{}")
    inputs = inputs or {}

    run = PipelineRun(
        pipeline_id=pipeline.id,
        external_id=None,
        status="queued",
        inputs=json.dumps(inputs),
        triggered_by_user_id=user_id,
    )
    db.add(run)
    db.flush()

    try:
        secret = _resolve_secret(cfg, pipeline)
        result = await adapter.trigger(config=config, secret=secret, inputs=inputs)
    except (PipelineProviderError, PipelineUnreachable) as e:
        run.status = "failed"
        run.error = str(e)
        run.completed_at = datetime.now(UTC)
        db.flush()
        return run

    run.external_id = result.external_id
    run.status = result.status
    run.html_url = result.html_url
    run.last_polled_at = datetime.now(UTC)
    db.flush()
    return run


async def refresh_run(cfg: Config, db: Session, run: PipelineRun) -> PipelineRun:
    """Poll the provider for the run's current status and persist."""
    if run.status in ("success", "failed", "cancelled", "skipped") and run.completed_at:
        return run
    pipeline = run.pipeline if hasattr(run, "pipeline") and run.pipeline else db.get(Pipeline, run.pipeline_id)
    if pipeline is None:
        run.status = "failed"
        run.error = "pipeline deleted"
        db.flush()
        return run
    adapter = get_adapter(pipeline.provider)
    config = json.loads(pipeline.config or "{}")
    try:
        secret = _resolve_secret(cfg, pipeline)
        result: PollResult = await adapter.poll(
            config=config, secret=secret, external_id=run.external_id
        )
    except (PipelineProviderError, PipelineUnreachable) as e:
        run.error = str(e)
        run.last_polled_at = datetime.now(UTC)
        db.flush()
        return run

    run.status = result.status
    run.last_polled_at = datetime.now(UTC)
    if result.started_at and not run.started_at:
        run.started_at = result.started_at
    if result.completed_at and run.status in ("success", "failed", "cancelled", "skipped"):
        run.completed_at = result.completed_at
    if result.html_url:
        run.html_url = result.html_url
    if result.status not in ("queued", "running", "unknown"):
        # Terminal status - clear stale error so the row reads cleanly if a
        # later poll succeeds after a transient failure.
        run.error = None
    db.flush()
    return run
