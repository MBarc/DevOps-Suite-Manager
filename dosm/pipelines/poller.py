"""Background pipeline-run poller.

Wakes on a short tick, selects non-terminal PipelineRun rows that are due for
their next poll (age-based cadence), calls repo.refresh_run against each
adapter in parallel (bounded concurrency), and persists status transitions.

Abandoned runs (no movement after poller_abandon_after_hours) are marked
failed so they don't accumulate forever.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from dosm.config import Config
from dosm.db import session_scope
from dosm.models import AuditLog, Pipeline, PipelineRun
from dosm.pipelines import repo
from dosm.recording import events as rec_events

log = logging.getLogger(__name__)

NON_TERMINAL = ("queued", "running", "unknown")


def _poll_cadence_seconds(age_seconds: float) -> float:
    """How long to wait between polls based on how old the run is."""
    if age_seconds < 60:
        return 5.0
    if age_seconds < 300:
        return 15.0
    if age_seconds < 1800:
        return 60.0
    if age_seconds < 14400:
        return 180.0
    return 300.0


@dataclass
class TickStats:
    polled: int = 0
    transitioned: int = 0
    abandoned: int = 0
    errors: int = 0


async def _poll_one(cfg: Config, run_id: int) -> bool:
    """Poll a single run in its own DB session. Returns True on status change."""
    try:
        with session_scope() as db:
            run = db.get(PipelineRun, run_id)
            if run is None:
                return False
            if run.status not in NON_TERMINAL:
                return False
            pipeline = db.get(Pipeline, run.pipeline_id)
            old_status = run.status
            await repo.refresh_run(cfg, db, run)
            if run.status == old_status:
                return False
            db.add(
                AuditLog(
                    actor_id=None,
                    action="pipeline.run.status",
                    target=f"pipeline_run:{run.id}",
                    details=f"{old_status} to {run.status}",
                )
            )
            if run.triggered_by_user_id and pipeline:
                rec_events.record_pipeline_status(
                    run.triggered_by_user_id, pipeline.name, run.id, run.status
                )
            return True
    except Exception:
        log.exception("poller: error polling run %d", run_id)
        return False


async def poll_tick(cfg: Config) -> TickStats:
    """One poller tick: abandon stale runs, then poll all due runs."""
    stats = TickStats()
    now = datetime.now(UTC)
    abandon_before = now - timedelta(hours=cfg.pipelines.poller_abandon_after_hours)

    # Collect due run IDs and handle abandonment in a single short session.
    due_run_ids: list[int] = []
    try:
        with session_scope() as db:
            runs = list(
                db.execute(
                    select(PipelineRun).where(
                        PipelineRun.status.in_(NON_TERMINAL),
                        PipelineRun.external_id.isnot(None),
                    )
                ).scalars()
            )
            for run in runs:
                age = (now - run.triggered_at).total_seconds()
                if run.triggered_at < abandon_before:
                    run.status = "failed"
                    run.error = (
                        f"abandoned: no provider response after "
                        f"{cfg.pipelines.poller_abandon_after_hours}h"
                    )
                    run.completed_at = now
                    db.flush()
                    stats.abandoned += 1
                    log.warning("poller: abandoned run %d (age=%.0fh)", run.id, age / 3600)
                    continue
                cadence = _poll_cadence_seconds(age)
                if run.last_polled_at is None:
                    due_run_ids.append(run.id)
                elif (now - run.last_polled_at).total_seconds() >= cadence:
                    due_run_ids.append(run.id)
    except Exception:
        log.exception("poller: error in selection/abandon phase")
        return stats

    if not due_run_ids:
        return stats

    # Poll due runs in parallel, bounded by max_concurrent.
    sem = asyncio.Semaphore(cfg.pipelines.poller_max_concurrent)

    async def _bounded(run_id: int) -> bool:
        async with sem:
            return await _poll_one(cfg, run_id)

    results = await asyncio.gather(*(_bounded(rid) for rid in due_run_ids), return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            stats.errors += 1
        elif r:
            stats.transitioned += 1
    stats.polled = len(due_run_ids)
    return stats


async def pipeline_poll_loop(cfg: Config) -> None:
    """Runs forever. Started from app startup; cancellation-safe."""
    log.info(
        "pipeline poller started (tick=%.1fs, max_concurrent=%d, abandon=%dh)",
        cfg.pipelines.poller_tick_seconds,
        cfg.pipelines.poller_max_concurrent,
        cfg.pipelines.poller_abandon_after_hours,
    )
    while True:
        try:
            await asyncio.sleep(cfg.pipelines.poller_tick_seconds)
            stats = await poll_tick(cfg)
            if stats.polled or stats.abandoned:
                log.debug(
                    "poller tick: polled=%d transitioned=%d abandoned=%d errors=%d",
                    stats.polled, stats.transitioned, stats.abandoned, stats.errors,
                )
        except asyncio.CancelledError:
            log.info("pipeline poller stopped")
            return
        except Exception:
            # Never let the loop die on a transient error.
            log.exception("poller: unexpected error in tick loop")
