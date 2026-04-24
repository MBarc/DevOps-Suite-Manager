from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from dosm.auth.deps import _NotAuthenticated, get_current_user, require_user
from dosm.db import get_session
from dosm.models import AuditLog, User
from dosm.terminals.discover import discover_shells, find_shell
from dosm.terminals.pty_bridge import open_pty
from dosm.terminals.recorder import AsciinemaRecorder, recording_path

router = APIRouter(prefix="/terminals")


def _require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="terminals require admin role")
    return user


def _templates(request: Request):
    return request.app.state.templates


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def terminals_index(request: Request, user: User = Depends(_require_admin)):
    cfg = request.app.state.config
    if not cfg.terminals.enabled:
        raise HTTPException(status_code=404)
    shells = discover_shells(cfg.terminals)
    return _templates(request).TemplateResponse(
        request,
        "terminals/list.html",
        {
            "shells": shells,
            "record_by_default": cfg.terminals.record_by_default,
            "user": user,
        },
    )


@router.get("/{shell_id}", response_class=HTMLResponse, include_in_schema=False)
async def terminals_session(
    shell_id: str,
    request: Request,
    user: User = Depends(_require_admin),
    record: int = 1,
):
    cfg = request.app.state.config
    if not cfg.terminals.enabled:
        raise HTTPException(status_code=404)
    shell = find_shell(discover_shells(cfg.terminals), shell_id)
    if shell is None:
        raise HTTPException(404)
    return _templates(request).TemplateResponse(
        request,
        "terminals/session.html",
        {
            "shell": shell,
            "record": bool(record),
            "user": user,
        },
    )


@router.websocket("/ws/{shell_id}")
async def terminals_ws(
    websocket: WebSocket,
    shell_id: str,
    record: int = 1,
    cols: int = 80,
    rows: int = 24,
):
    """Bidirectional bridge between xterm.js (client) and a local PTY.

    Inbound messages (from browser) are JSON of shape:
      {"type": "input", "data": "..."}
      {"type": "resize", "rows": N, "cols": N}

    Outbound messages (to browser):
      {"type": "output", "data": "..."}
      {"type": "exit"}
      {"type": "error", "data": "..."}
    """
    # Authn/authz: rely on the session cookie Starlette parses onto websocket.session.
    uid = websocket.session.get("user_id") if websocket.session else None
    if uid is None:
        await websocket.close(code=4401)
        return

    # Re-open a DB session to resolve user + record audit trail.
    from dosm.db import get_engine
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=get_engine(), future=True)
    with Session() as s:
        user = s.get(User, uid)
        if user is None or not user.is_active or user.role != "admin":
            await websocket.close(code=4403)
            return
        user_id = user.id
        username = user.username

    cfg = websocket.app.state.config
    shell = find_shell(discover_shells(cfg.terminals), shell_id)
    if shell is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()

    session_id = uuid.uuid4().hex[:12]
    pty = open_pty(shell.command, env=shell.env, cwd=shell.cwd, rows=rows, cols=cols)

    recorder: AsciinemaRecorder | None = None
    if record and cfg.terminals.record_by_default or record:
        # Always record when the query param is set; config default only
        # decides what the launch page pre-checks.
        rec_root = cfg.home / cfg.terminals.recordings_dir
        rec_path = recording_path(rec_root, f"{username}-{shell.id}-{session_id}")
        recorder = AsciinemaRecorder(
            rec_path,
            cols=cols,
            rows=rows,
            command=" ".join(shell.command),
            title=f"{username}@dosm :: {shell.name}",
            env={"TERM": "xterm-256color"},
        )

    # Audit.
    with Session() as s:
        s.add(
            AuditLog(
                actor_id=user_id,
                action="terminal.start",
                target=f"shell:{shell.id}",
                details=(
                    f"session={session_id} recording={recorder.path.name if recorder else 'none'}"
                ),
            )
        )
        s.commit()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    async def pump_pty_to_ws() -> None:
        while not stop.is_set() and pty.alive:
            data = await loop.run_in_executor(None, pty.read, 4096)
            if not data:
                break
            if recorder is not None:
                recorder.record_output(data)
            try:
                await websocket.send_text(
                    json.dumps({"type": "output", "data": data.decode("utf-8", errors="replace")})
                )
            except Exception:
                break
        stop.set()

    async def pump_ws_to_pty() -> None:
        while not stop.is_set():
            try:
                msg = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception:
                break
            try:
                obj = json.loads(msg)
            except json.JSONDecodeError:
                continue
            kind = obj.get("type")
            if kind == "input":
                data = obj.get("data", "").encode("utf-8")
                if recorder is not None:
                    recorder.record_input(data)
                pty.write(data)
            elif kind == "resize":
                r = int(obj.get("rows", 24))
                c = int(obj.get("cols", 80))
                pty.resize(r, c)
                if recorder is not None:
                    recorder.resize(r, c)
        stop.set()

    try:
        await asyncio.gather(pump_pty_to_ws(), pump_ws_to_pty())
    finally:
        pty.close()
        if recorder is not None:
            recorder.close()
        try:
            await websocket.send_text(json.dumps({"type": "exit"}))
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
        with Session() as s:
            s.add(
                AuditLog(
                    actor_id=user_id,
                    action="terminal.end",
                    target=f"shell:{shell.id}",
                    details=f"session={session_id}",
                )
            )
            s.commit()
