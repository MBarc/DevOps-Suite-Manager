"""Background Confluence listener poller.

Wakes on a tick, selects enabled listeners (cross-tenant) whose last sync is
older than ``min_resync_seconds``, and reconciles each via ``sync_listener``
under bounded concurrency. Mirrors ``pipelines.poller``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from dosm.config import Config
from dosm.confluence import repo
from dosm.confluence.sync import sync_listener
from dosm.db import session_scope
from dosm.models import ConfluenceListener

log = logging.getLogger(__name__)


@dataclass
class TickStats:
    synced: int = 0
    changed: int = 0
    errors: int = 0


def _aware(dt: datetime | None) -> datetime | None:
    """Normalize a possibly-naive DB datetime to UTC-aware for comparison."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def _sync_one(cfg: Config, listener_id: int) -> tuple[int, int]:
    """Sync one listener in its own session. Returns (changed, errors)."""
    try:
        with session_scope() as db:
            listener = db.get(ConfluenceListener, listener_id)
            if listener is None or not listener.enabled:
                return 0, 0
            result = await sync_listener(cfg, listener, db)
            return result.changed, len(result.errors)
    except Exception:
        log.exception("confluence poller: error syncing listener %d", listener_id)
        return 0, 1


async def poll_tick(cfg: Config) -> TickStats:
    stats = TickStats()
    now = datetime.now(UTC)
    min_age = timedelta(seconds=cfg.confluence.min_resync_seconds)

    due: list[int] = []
    try:
        with session_scope() as db:
            for listener in repo.list_enabled(db, tid=None):
                last = _aware(listener.last_synced_at)
                if last is None or (now - last) >= min_age:
                    due.append(listener.id)
    except Exception:
        log.exception("confluence poller: selection error")
        return stats

    if not due:
        return stats

    sem = asyncio.Semaphore(cfg.confluence.poller_max_concurrent)

    async def _bounded(lid: int) -> tuple[int, int]:
        async with sem:
            return await _sync_one(cfg, lid)

    results = await asyncio.gather(*(_bounded(lid) for lid in due), return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            stats.errors += 1
        else:
            changed, errs = r
            stats.synced += 1
            stats.changed += changed
            stats.errors += errs
    return stats


async def confluence_poll_loop(cfg: Config) -> None:
    """Runs forever. Started from app startup; cancellation-safe."""
    log.info(
        "confluence poller started (tick=%.1fs, max_concurrent=%d)",
        cfg.confluence.poller_tick_seconds,
        cfg.confluence.poller_max_concurrent,
    )
    while True:
        try:
            await asyncio.sleep(cfg.confluence.poller_tick_seconds)
            stats = await poll_tick(cfg)
            if stats.changed or stats.errors:
                log.debug(
                    "confluence tick: synced=%d changed=%d errors=%d",
                    stats.synced, stats.changed, stats.errors,
                )
        except asyncio.CancelledError:
            log.info("confluence poller stopped")
            return
        except Exception:
            log.exception("confluence poller: unexpected error in tick loop")
