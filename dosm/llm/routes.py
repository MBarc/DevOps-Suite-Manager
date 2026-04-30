from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.agent.actions import classify_command, get_action, list_actions
from dosm.agent.prompt import (
    agent_system_prompt,
    strip_plan_blocks,
    strip_query_blocks,
    tools_for_agent,
)
from dosm.agent.queries import get_query
from dosm.auth.deps import require_user
from dosm.db import get_session, session_scope
from dosm.llm.ollama import OllamaClient, OllamaError, OllamaUnreachable
from dosm.llm.retrieval import (
    Citation,
    citations_to_json,
    compose_context_block,
    retrieve,
)
from dosm.models import AuditLog, ChatMessage, Conversation, Host, PlanCard, User

router = APIRouter(prefix="/chat")

MAX_HISTORY_TURNS = 12
MAX_QUERY_ROUNDS = 5

_COMMAND_CLASSIFY_TOOLS = {"ssh_exec", "local_exec", "winrm_exec"}

# Process-local registry of in-flight reply generations.
# key: conv_id  →  (reply_to_msg_id, Future[result_dict], Queue[SSE bytes | None])
# The asyncio.Task runs independently of the SSE connection, so navigating
# away and coming back reconnects to the same generation rather than
# restarting it.
_reply_futures: dict[int, tuple[int, asyncio.Future, asyncio.Queue]] = {}


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
    conv = Conversation(user_id=user.id, title="New chat", mode="agent")
    db.add(conv)
    db.flush()
    cid = conv.id
    return RedirectResponse(f"/chat/{cid}", status_code=303)


@router.post("/{cid}/rename", include_in_schema=False)
async def chat_rename(
    cid: int,
    title: str = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    conv = db.get(Conversation, cid)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(404)
    title = title.strip()[:255] or "New chat"
    conv.title = title
    db.add(AuditLog(actor_id=user.id, action="chat.rename", target=f"conversation:{cid}", details=title))
    db.commit()
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
    db.commit()
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

    plan_rows = list(
        db.execute(
            select(PlanCard).where(PlanCard.conversation_id == cid).order_by(PlanCard.id)
        ).scalars()
    )
    cards_by_msg: dict[int, list[dict]] = {}
    for c in plan_rows:
        try:
            args = json.loads(c.effective_args or c.args or "{}")
        except json.JSONDecodeError:
            args = {}
        try:
            result = json.loads(c.result) if c.result else None
        except json.JSONDecodeError:
            result = None
        action_spec = get_action(c.tool)
        cards_by_msg.setdefault(c.message_id or 0, []).append(
            {
                "card": c,
                "args": args,
                "result": result,
                "schema": action_spec.args_schema if action_spec else [],
            }
        )

    # confirm_fields: tool name → elevated_confirm_field name, for template use.
    confirm_fields: dict[str, str] = {
        spec.name: spec.elevated_confirm_field
        for spec in list_actions()
        if spec.elevated_confirm_field
    }

    hydrated = []
    for m in messages:
        cits = []
        if m.citations:
            try:
                cits = json.loads(m.citations)
            except json.JSONDecodeError:
                cits = []
        thinking = []
        if m.thinking:
            try:
                thinking = json.loads(m.thinking)
            except json.JSONDecodeError:
                thinking = []
        thinking_total_ms = sum(int(t.get("elapsed_ms") or 0) for t in thinking)
        msg_cards = cards_by_msg.get(m.id, [])
        pending_safe_count = sum(
            1
            for cd in msg_cards
            if cd["card"].status == "pending" and cd["card"].tier == "safe"
        )
        hydrated.append({
            "m": m,
            "citations": cits,
            "thinking": thinking,
            "thinking_total_ms": thinking_total_ms,
            "generation_ms": m.generation_ms,
            "cards": msg_cards,
            "pending_safe_count": pending_safe_count,
        })

    # Detect an in-flight generation so the template can auto-start SSE on load.
    auto_reply_to: int | None = None
    interrupted_msg_id: int | None = None
    entry = _reply_futures.get(cid)
    if entry is not None:
        pending_reply_to, pending_future, _ = entry
        if not pending_future.done():
            auto_reply_to = pending_reply_to
    else:
        # After a container restart _reply_futures is empty. Detect a dangling
        # user message (no assistant reply follows it) so the template can show
        # a Retry button.
        if messages and messages[-1].role == "user":
            interrupted_msg_id = messages[-1].id

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
            "is_agent": conv.mode == "agent",
            "elevated_card_id": int(request.query_params.get("elevated_card", "0")) or None,
            "confirm_fields": confirm_fields,
            "auto_reply_to": auto_reply_to,
            "interrupted_msg_id": interrupted_msg_id,
        },
    )


# --- Post a user message --------------------------------------------------


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
    conv.updated_at = datetime.now(UTC)
    return RedirectResponse(f"/chat/{cid}?reply_to={user_msg_id}", status_code=303)


# --- SSE helpers ----------------------------------------------------------


def _sse(event: str | None, data: str) -> bytes:
    lines = []
    if event:
        lines.append(f"event: {event}")
    for ln in data.splitlines() or [""]:
        lines.append(f"data: {ln}")
    lines.append("")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


# --- Background generation task -------------------------------------------


async def _generate_reply(
    *,
    conv_id: int,
    future: asyncio.Future,
    queue: asyncio.Queue,
    history_for_llm: list[dict],
    citations_payload: list,
    next_ord: int,
    actor_id: int,
    cfg,
    agent_tools: list[dict],
    client: OllamaClient,
) -> None:
    """Runs LLM generation independently of any SSE connection.

    Puts live SSE bytes into *queue* so a connected client can watch
    thinking steps as they happen.  Sets *future* with the final result
    dict once the assistant message has been written to the DB.  Always
    puts a ``None`` sentinel into the queue last so any drain loop exits.
    """
    llm_messages = list(history_for_llm)
    final_content = ""
    error_text: str | None = None
    thinking_trace: list[dict] = []
    pending_plan_cards: list[dict] = []
    generation_start = time.perf_counter()

    try:
        for _round in range(MAX_QUERY_ROUNDS + 1):
            try:
                resp = await client.complete_chat(
                    llm_messages, tools=agent_tools, num_ctx=16384
                )
            except OllamaUnreachable as e:
                error_text = f"Cannot reach Ollama at {cfg.llm.base_url}. Is the server running?\n{e}"
                await queue.put(_sse("error", error_text))
                break
            except OllamaError as e:
                error_text = f"Ollama error: {e}"
                await queue.put(_sse("error", error_text))
                break
            except Exception as e:
                error_text = f"Unexpected: {type(e).__name__}: {e}"
                await queue.put(_sse("error", error_text))
                break

            final_content = resp.content

            # No tool calls (or round cap reached) — this is the final answer.
            if not resp.tool_calls or _round == MAX_QUERY_ROUNDS:
                break

            # Re-insert the assistant message (with tool_calls) before tool results.
            llm_messages.append(resp.raw_message)

            action_called = False
            for tc in resp.tool_calls:
                # Safety net: local_exec has no 'host' parameter. If the model
                # passes one anyway it has confused the execution context. Inject
                # a correction as a tool result so the LLM can self-correct on
                # the next round rather than silently running on the DOSM container.
                if tc.name == "local_exec" and (tc.arguments.get("host") or "").strip():
                    _host_name = (tc.arguments.get("host") or "").strip()
                    _cmd = (tc.arguments.get("command") or "").strip()
                    _correction = (
                        f"Error: local_exec has no 'host' parameter — drop it. "
                        f"Re-examine what the user actually asked: "
                        f"(A) If they want DOSM to run a connectivity check TO '{_host_name}' "
                        f"(e.g. 'ping {_host_name}', 'can you reach {_host_name}'), "
                        f"call local_exec with only the command (no host argument). "
                        f"The command may reference '{_host_name}' by its real address — "
                        f"call list_hosts first if you need the hostname or IP. "
                        f"(B) If they want '{_host_name}' itself to run the command "
                        f"(e.g. 'run df on {_host_name}', 'from {_host_name} ping X'), "
                        f"use ssh_exec with host='{_host_name}' and no local_exec at all. "
                        f"The original command was: {_cmd!r}. Choose (A) or (B) and retry."
                    )
                    _step = len(thinking_trace)
                    _entry = {
                        "tool": "local_exec",
                        "args": tc.arguments,
                        "ok": False,
                        "summary": f"local_exec called with host='{_host_name}' — nudging LLM to self-correct",
                        "data_preview": _correction,
                        "elapsed_ms": 0,
                    }
                    thinking_trace.append(_entry)
                    await queue.put(_sse("query_result", json.dumps({"step": _step, **_entry})))
                    llm_messages.append({"role": "tool", "content": _correction})
                    continue

                query_spec = get_query(tc.name)
                action_spec = get_action(tc.name)
                step_idx = len(thinking_trace)

                if query_spec:
                    await queue.put(_sse(
                        "query_call",
                        json.dumps({"step": step_idx, "tool": tc.name, "args": tc.arguments}),
                    ))
                    t0 = time.perf_counter()
                    try:
                        result = await query_spec.runner(cfg, tc.arguments)
                        elapsed_ms = int((time.perf_counter() - t0) * 1000)
                        preview = (result.data or "")[:1500] if result.data else None
                        entry = {
                            "tool": tc.name,
                            "args": tc.arguments,
                            "ok": result.ok,
                            "summary": result.summary,
                            "data_preview": preview,
                            "elapsed_ms": elapsed_ms,
                        }
                        thinking_trace.append(entry)
                        await queue.put(_sse("query_result", json.dumps({"step": step_idx, **entry})))
                        llm_messages.append({"role": "tool", "content": result.to_llm_text()})
                    except Exception as exc:
                        elapsed_ms = int((time.perf_counter() - t0) * 1000)
                        summary = f"{type(exc).__name__}: {exc}"
                        entry = {
                            "tool": tc.name,
                            "args": tc.arguments,
                            "ok": False,
                            "summary": summary,
                            "data_preview": None,
                            "elapsed_ms": elapsed_ms,
                        }
                        thinking_trace.append(entry)
                        await queue.put(_sse("query_result", json.dumps({"step": step_idx, **entry})))
                        llm_messages.append({"role": "tool", "content": f"ERROR: {summary}"})

                elif action_spec:
                    if tc.name in _COMMAND_CLASSIFY_TOOLS:
                        tier = classify_command(cfg, tc.arguments.get("command", ""))
                    else:
                        tier = action_spec.classify(tc.arguments)
                    pending_plan_cards.append({
                        "tool": tc.name,
                        "args": tc.arguments,
                        "tier": tier,
                    })
                    action_called = True

                else:
                    summary = f"unknown tool {tc.name!r}"
                    entry = {
                        "tool": tc.name,
                        "args": tc.arguments,
                        "ok": False,
                        "summary": summary,
                        "data_preview": None,
                        "elapsed_ms": 0,
                    }
                    thinking_trace.append(entry)
                    await queue.put(_sse("query_result", json.dumps({"step": step_idx, **entry})))
                    llm_messages.append({"role": "tool", "content": f"ERROR: {summary}"})

            if action_called:
                # Model's content from this round is the wrap-up; plan cards follow.
                break

        # --- Persist to DB ---
        generation_ms = int((time.perf_counter() - generation_start) * 1000)
        plan_card_payloads: list[dict] = []
        with session_scope() as s:
            assistant_msg = ChatMessage(
                conversation_id=conv_id,
                role="assistant",
                content=final_content,
                citations=json.dumps(citations_payload),
                thinking=json.dumps(thinking_trace) if thinking_trace else None,
                error=error_text,
                generation_ms=generation_ms,
                ord=next_ord,
            )
            s.add(assistant_msg)
            s.flush()

            for pc in pending_plan_cards:
                card = PlanCard(
                    conversation_id=conv_id,
                    message_id=assistant_msg.id,
                    tool=pc["tool"],
                    args=json.dumps(pc["args"]),
                    tier=pc["tier"],
                )
                s.add(card)
                s.flush()
                plan_card_payloads.append({
                    "id": card.id,
                    "tool": card.tool,
                    "args": pc["args"],
                    "tier": pc["tier"],
                })

            conv_row = s.get(Conversation, conv_id)
            if conv_row is not None:
                conv_row.updated_at = datetime.now(UTC)
                if not conv_row.model:
                    conv_row.model = cfg.llm.model
            s.add(
                AuditLog(
                    actor_id=actor_id,
                    action="chat.reply",
                    target=f"conversation:{conv_id}",
                    details=(
                        f"citations={len(citations_payload)} plans={len(plan_card_payloads)}"
                        + (f" error={error_text[:80]}" if error_text else "")
                    ),
                )
            )

        if not future.done():
            future.set_result({
                "content": final_content,
                "error": error_text,
                "plan_cards": plan_card_payloads,
            })

    except Exception as exc:
        if not future.done():
            future.set_exception(exc)
    finally:
        _reply_futures.pop(conv_id, None)
        await queue.put(None)  # sentinel — tells drain loops to exit


# --- SSE stream -----------------------------------------------------------


def _stream_result(result: dict):
    """Async generator that emits the final answer SSE events from a result dict."""

    async def _gen():
        if result.get("content"):
            yield _sse("token", result["content"])
        if result.get("error"):
            yield _sse("error", result["error"])
        for payload in result.get("plan_cards", []):
            yield _sse("plan", json.dumps(payload))
        yield _sse("done", "")

    return _gen()


@router.get("/{cid}/stream", include_in_schema=False)
async def chat_stream(
    cid: int,
    request: Request,
    reply_to: int,
    user: User = Depends(require_user),
):
    cfg = request.app.state.config

    # --- Reconnect to an in-flight generation ---
    existing = _reply_futures.get(cid)
    if existing is not None:
        existing_reply_to, future, _queue = existing
        if existing_reply_to == reply_to and not future.done():
            async def reconnect_gen():
                try:
                    result = await asyncio.shield(future)
                except Exception as e:
                    yield _sse("error", f"Generation failed: {e}")
                    yield _sse("done", "")
                    return
                async for chunk in _stream_result(result):
                    yield chunk

            return StreamingResponse(
                reconnect_gen(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    # --- Fresh generation ---
    with session_scope() as s:
        conv = s.get(Conversation, cid)
        if conv is None or conv.user_id != user.id:
            raise HTTPException(404)
        messages = _latest_messages(s, cid)
        triggering = next((m for m in messages if m.id == reply_to), None)
        if triggering is None or triggering.role != "user":
            raise HTTPException(400, "reply_to must reference a user message in this conversation")
        if any(m.role == "assistant" and m.ord > triggering.ord for m in messages):
            raise HTTPException(409, "assistant already replied to this message")

        query = triggering.content
        citations: list[Citation] = retrieve(s, cfg, query, k=5)
        citations_payload = citations_to_json(citations)
        ctx_block = compose_context_block(citations)
        sys_prompt = agent_system_prompt(user.username)

        # Inject concrete host context for any inventory hosts named in the query.
        # A 3B model won't reliably infer execution context from abstract rules alone;
        # showing "protocol=ssh" next to the host name makes the right tool obvious.
        _query_lower = query.lower()
        _all_hosts = list(s.execute(select(Host)).scalars())
        _mentioned = [h for h in _all_hosts if h.name.lower() in _query_lower]
        if _mentioned:
            _host_lines = "\n".join(
                f"  {h.name}: address={h.hostname}, port={h.port}, protocol={h.protocol}"
                for h in _mentioned
            )
            sys_prompt += (
                f"\n\nInventory hosts referenced in this request:\n{_host_lines}\n"
                f"Use ssh_exec when the request asks what happens ON the host (the host is the executor). "
                f"Use winrm_exec for protocol=winrm hosts. "
                f"Use local_exec (no host arg) when DOSM itself performs the operation "
                f"— e.g. pinging or port-checking a host FROM DOSM. "
                f"A single request can require both: e.g. 'ping herupa' (local_exec) "
                f"AND 'from herupa ping 8.8.8.8' (ssh_exec) — generate both tool calls."
            )

        history_for_llm = [{"role": "system", "content": f"{sys_prompt}\n\n{ctx_block}"}]
        for m in messages[-MAX_HISTORY_TURNS:]:
            if m.role == "user":
                history_for_llm.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                # Strip any legacy XML blocks so old messages don't confuse
                # tool-calling models that were never trained on that format.
                clean = strip_plan_blocks(strip_query_blocks(m.content))
                history_for_llm.append({"role": "assistant", "content": clean})
        next_ord = (messages[-1].ord + 1) if messages else 0
        actor_id = user.id
        conv_id = conv.id

    client = OllamaClient(base_url=cfg.llm.base_url, model=cfg.llm.model, timeout=300.0)
    agent_tools = tools_for_agent()

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    queue: asyncio.Queue = asyncio.Queue()
    _reply_futures[conv_id] = (reply_to, future, queue)

    asyncio.create_task(_generate_reply(
        conv_id=conv_id,
        future=future,
        queue=queue,
        history_for_llm=history_for_llm,
        citations_payload=citations_payload,
        next_ord=next_ord,
        actor_id=actor_id,
        cfg=cfg,
        agent_tools=agent_tools,
        client=client,
    ))

    async def gen():
        yield _sse("citations", json.dumps(citations_payload))

        # Drain live thinking events until the background task signals done.
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

        # Emit the final answer once the DB write has committed.
        try:
            result = await asyncio.shield(future)
        except Exception as e:
            yield _sse("error", f"Generation failed: {e}")
            yield _sse("done", "")
            return

        async for chunk in _stream_result(result):
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
