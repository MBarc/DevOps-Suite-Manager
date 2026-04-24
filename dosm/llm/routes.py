from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session, session_scope
from dosm.llm.ollama import OllamaClient, OllamaError, OllamaUnreachable
from dosm.llm.retrieval import (
    Citation,
    citations_to_json,
    compose_context_block,
    compose_system_prompt,
    retrieve,
)
from dosm.models import AuditLog, ChatMessage, Conversation, User

router = APIRouter(prefix="/chat")

MAX_HISTORY_TURNS = 12


def _templates(request: Request):
    return request.app.state.templates


def _latest_messages(db: Session, conv_id: int) -> list[ChatMessage]:
    return list(
        db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conv_id)
            .order_by(ChatMessage.ord.asc())
        ).scalars()
    )


def _auto_title(message: str) -> str:
    t = message.strip().splitlines()[0] if message.strip() else "New chat"
    return (t[:80] + "…") if len(t) > 80 else t


# --- List + create --------------------------------------------------------


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def chat_home(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    convs = list(
        db.execute(
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(Conversation.updated_at.desc())
        ).scalars()
    )
    return _templates(request).TemplateResponse(
        request,
        "chat/list.html",
        {"conversations": convs, "user": user, "active_id": None},
    )


@router.post("/new", include_in_schema=False)
async def chat_new(
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    conv = Conversation(user_id=user.id, title="New chat")
    db.add(conv)
    db.flush()
    cid = conv.id
    return RedirectResponse(f"/chat/{cid}", status_code=303)


@router.post("/{cid}/delete", include_in_schema=False)
async def chat_delete(
    cid: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    conv = db.get(Conversation, cid)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(404)
    db.delete(conv)
    db.add(AuditLog(actor_id=user.id, action="chat.delete", target=f"conversation:{cid}"))
    return RedirectResponse("/chat", status_code=303)


# --- View conversation ----------------------------------------------------


@router.get("/{cid}", response_class=HTMLResponse, include_in_schema=False)
async def chat_view(
    cid: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    conv = db.get(Conversation, cid)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(404)

    convs = list(
        db.execute(
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(Conversation.updated_at.desc())
        ).scalars()
    )
    messages = _latest_messages(db, cid)
    # parse citations JSON once for the template
    hydrated = []
    for m in messages:
        cits = []
        if m.citations:
            try:
                cits = json.loads(m.citations)
            except json.JSONDecodeError:
                cits = []
        hydrated.append({"m": m, "citations": cits})
    return _templates(request).TemplateResponse(
        request,
        "chat/conversation.html",
        {
            "user": user,
            "conversation": conv,
            "conversations": convs,
            "messages": hydrated,
            "active_id": cid,
            "ollama_model": request.app.state.config.llm.model,
        },
    )


# --- Post a user message (non-streaming HTTP POST) -------------------------


@router.post("/{cid}/message", include_in_schema=False)
async def chat_post_message(
    cid: int,
    request: Request,
    content: str = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    conv = db.get(Conversation, cid)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(404)

    content = content.strip()
    if not content:
        return RedirectResponse(f"/chat/{cid}", status_code=303)

    existing = _latest_messages(db, cid)
    next_ord = (existing[-1].ord + 1) if existing else 0
    msg = ChatMessage(conversation_id=cid, role="user", content=content, ord=next_ord)
    db.add(msg)
    db.flush()
    user_msg_id = msg.id
    if conv.title == "New chat" or not existing:
        conv.title = _auto_title(content)
    conv.updated_at = datetime.utcnow()
    return RedirectResponse(f"/chat/{cid}?reply_to={user_msg_id}", status_code=303)


# --- SSE stream: generate assistant reply for a given user message ---------


def _sse(event: str | None, data: str) -> bytes:
    lines = []
    if event:
        lines.append(f"event: {event}")
    for ln in data.splitlines() or [""]:
        lines.append(f"data: {ln}")
    lines.append("")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


@router.get("/{cid}/stream", include_in_schema=False)
async def chat_stream(
    cid: int,
    request: Request,
    reply_to: int,
    user: User = Depends(require_user),
):
    # All DB work happens in short-lived sessions inside the generator so we
    # don't hold a connection across the streaming lifetime.
    cfg = request.app.state.config

    with session_scope() as s:
        conv = s.get(Conversation, cid)
        if conv is None or conv.user_id != user.id:
            raise HTTPException(404)
        messages = _latest_messages(s, cid)
        triggering = next((m for m in messages if m.id == reply_to), None)
        if triggering is None or triggering.role != "user":
            raise HTTPException(400, "reply_to must reference a user message in this conversation")
        # Make sure we don't double-generate.
        if any(m.role == "assistant" and m.ord > triggering.ord for m in messages):
            raise HTTPException(409, "assistant already replied to this message")

        query = triggering.content
        citations: list[Citation] = retrieve(s, cfg, query, k=5)
        citations_payload = citations_to_json(citations)

        # Build chat-style messages for Ollama, with RAG context injected
        # into the system prompt.
        sys_prompt = compose_system_prompt(user.username)
        ctx_block = compose_context_block(citations)
        history_for_llm = [{"role": "system", "content": f"{sys_prompt}\n\n{ctx_block}"}]
        for m in messages[-MAX_HISTORY_TURNS:]:
            if m.role in ("user", "assistant"):
                history_for_llm.append({"role": m.role, "content": m.content})
        next_ord = (messages[-1].ord + 1) if messages else 0
        actor_id = user.id
        conv_id = conv.id

    client = OllamaClient(base_url=cfg.llm.base_url, model=cfg.llm.model)

    async def gen():
        # Announce the citations upfront so the UI can render them immediately.
        yield _sse("citations", json.dumps(citations_payload))
        collected: list[str] = []
        error_text: str | None = None
        try:
            async for delta in client.stream_chat(history_for_llm):
                if delta.content:
                    collected.append(delta.content)
                    yield _sse("token", delta.content)
                if delta.done:
                    break
        except OllamaUnreachable as e:
            error_text = (
                f"Cannot reach Ollama at {cfg.llm.base_url}. Is the server running?\n{e}"
            )
            yield _sse("error", error_text)
        except OllamaError as e:
            error_text = f"Ollama error: {e}"
            yield _sse("error", error_text)
        except Exception as e:  # pragma: no cover — last-resort guardrail
            error_text = f"Unexpected: {type(e).__name__}: {e}"
            yield _sse("error", error_text)

        final_text = "".join(collected)
        with session_scope() as s:
            s.add(
                ChatMessage(
                    conversation_id=conv_id,
                    role="assistant",
                    content=final_text,
                    citations=json.dumps(citations_payload),
                    error=error_text,
                    ord=next_ord,
                )
            )
            conv = s.get(Conversation, conv_id)
            if conv is not None:
                conv.updated_at = datetime.utcnow()
                if not conv.model:
                    conv.model = cfg.llm.model
            s.add(
                AuditLog(
                    actor_id=actor_id,
                    action="chat.reply",
                    target=f"conversation:{conv_id}",
                    details=f"citations={len(citations_payload)}"
                    + (f" error={error_text[:80]}" if error_text else ""),
                )
            )
        yield _sse("done", "")

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
