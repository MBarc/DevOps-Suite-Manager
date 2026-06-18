from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.docs_index.indexer import reindex_async
from dosm.models import AuditLog, RecordingSession, User
from dosm.recording.journal import JournalWriter, RecordingOptions
from dosm.recording.state import (
    ActiveRecording,
    clear_active,
    get_active,
    set_active,
)

router = APIRouter(prefix="/recording")


def _slug(username: str) -> str:
    ts = time.strftime("%Y-%m-%d-%H%M")
    safe = "".join(c if c.isalnum() or c in "-_" else "" for c in username)[:16]
    return f"{ts}-{safe}"


# ---------------------------------------------------------------------------
# Startup helper - abort any sessions that were active when the process died.
# ---------------------------------------------------------------------------

def abort_stale_recordings(cfg) -> None:
    from sqlalchemy.orm import sessionmaker

    from dosm.db import get_engine

    S = sessionmaker(bind=get_engine(), future=True)
    with S() as db:
        rows = list(
            db.execute(
                select(RecordingSession).where(RecordingSession.status == "active")
            ).scalars()
        )
        for row in rows:
            slug = row.slug
            tmp_path = cfg.home / cfg.recording.tmp_dir / f"{slug}.md"
            final_rel = f"{cfg.recording.sessions_dir}/{slug}.md"
            final_path = cfg.home / final_rel
            if tmp_path.exists():
                try:
                    with open(tmp_path, "a", encoding="utf-8") as fh:
                        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
                        fh.write(
                            f"\n---\n\n"
                            f"*Recording aborted: server restarted at {ts}.*\n"
                        )
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path.rename(final_path)
                    row.journal_path = final_rel
                except Exception:
                    pass
            row.status = "aborted"
            row.stopped_at = datetime.now(UTC)
        db.commit()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class StartRequest(BaseModel):
    options: dict = {}


class EventRequest(BaseModel):
    kind: str           # "copy" | "paste" | "clipboard" | "guac_keystroke"
    direction: str = "" # e.g. "terminal copy", "ssh", "rdp"
    content: str = ""
    meta: str = ""      # extra context - host name for guac_keystroke events


class OptionsRequest(BaseModel):
    options: dict


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/status")
async def recording_status(
    user: User = Depends(require_user),
) -> JSONResponse:
    rec = get_active(user.id)
    if rec is None:
        return JSONResponse({"active": False})
    elapsed = int((datetime.now(UTC) - rec.started_at).total_seconds())
    return JSONResponse(
        {
            "active": True,
            "recording_id": rec.recording_id,
            "slug": rec.slug,
            "elapsed_seconds": elapsed,
            "options": rec.options.to_dict(),
        }
    )


@router.post("/start")
async def recording_start(
    body: StartRequest,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
) -> JSONResponse:
    cfg = request.app.state.config
    if not cfg.recording.enabled:
        raise HTTPException(403, "session recording is disabled")

    if get_active(user.id) is not None:
        raise HTTPException(409, "a recording is already active for this user")

    opts = RecordingOptions.from_dict(body.options)
    slug = _slug(user.username)
    tmp_path = cfg.home / cfg.recording.tmp_dir / f"{slug}.md"

    row = RecordingSession(
        user_id=user.id,
        slug=slug,
        options_json=json.dumps(opts.to_dict()),
        status="active",
    )
    db.add(row)
    db.flush()
    rec_id = row.id

    db.add(
        AuditLog(
            actor_id=user.id,
            action="recording.start",
            target=f"recording:{rec_id}",
            details=f"slug={slug}",
            ip=request.client.host if request.client else None,
        )
    )
    db.commit()

    writer = JournalWriter(tmp_path, slug=slug, username=user.username, options=opts)
    rec = ActiveRecording(
        recording_id=rec_id,
        user_id=user.id,
        slug=slug,
        options=opts,
        tmp_path=tmp_path,
        started_at=datetime.now(UTC),
        writer=writer,
    )
    set_active(user.id, rec)

    return JSONResponse({"recording_id": rec_id, "slug": slug})


@router.post("/stop")
async def recording_stop(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
) -> JSONResponse:
    cfg = request.app.state.config
    rec = clear_active(user.id)
    if rec is None:
        raise HTTPException(404, "no active recording for this user")

    final_rel = f"{cfg.recording.sessions_dir}/{rec.slug}.md"
    final_path = cfg.home / final_rel

    rec.writer.write_footer("finalized")
    rec.writer.close()

    final_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        rec.tmp_path.rename(final_path)
    except Exception:
        # Cross-device move fallback
        import shutil
        shutil.move(str(rec.tmp_path), str(final_path))

    row = db.get(RecordingSession, rec.recording_id)
    if row:
        row.status = "finalized"
        row.stopped_at = datetime.now(UTC)
        row.journal_path = final_rel

    db.add(
        AuditLog(
            actor_id=user.id,
            action="recording.stop",
            target=f"recording:{rec.recording_id}",
            details=f"slug={rec.slug} path={final_rel}",
            ip=request.client.host if request.client else None,
        )
    )
    db.commit()

    # Trigger a background reindex of just the sessions directory so the
    # new journal is immediately searchable in Docs / Chat.
    reindex_async(cfg, force=False)

    return JSONResponse(
        {
            "slug": rec.slug,
            "journal_path": final_rel,
            "doc_dir": cfg.recording.sessions_dir,
        }
    )


@router.post("/event")
async def recording_event(
    body: EventRequest,
    user: User = Depends(require_user),
) -> JSONResponse:
    """Accepts client-side events from the terminal and Guacamole pages."""
    from dosm.recording.events import record_clipboard, record_guac_command

    rec = get_active(user.id)
    if rec is None:
        return JSONResponse({"ok": False, "reason": "no active recording"})

    if body.kind in ("copy", "paste", "clipboard") and body.content:
        record_clipboard(user.id, body.direction or body.kind, body.content)
    elif body.kind == "guac_keystroke" and body.content:
        record_guac_command(user.id, body.direction, body.meta, body.content)

    return JSONResponse({"ok": True})


@router.put("/options")
async def recording_update_options(
    body: OptionsRequest,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
) -> JSONResponse:
    rec = get_active(user.id)
    if rec is None:
        raise HTTPException(404, "no active recording")

    new_opts = RecordingOptions.from_dict({**rec.options.to_dict(), **body.options})
    rec.writer.options = new_opts
    rec.options = new_opts

    row = db.get(RecordingSession, rec.recording_id)
    if row:
        row.options_json = json.dumps(new_opts.to_dict())
    db.commit()

    return JSONResponse({"ok": True, "options": new_opts.to_dict()})
