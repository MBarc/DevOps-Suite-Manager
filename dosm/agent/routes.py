from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.agent.actions import classify_command, get_action
from dosm.auth.deps import require_user
from dosm.db import get_session, session_scope
from dosm.models import AuditLog, ChatMessage, Conversation, Host, PlanCard, User
from dosm.recording import events as rec_events

router = APIRouter(prefix="/chat")


def _next_ord(db: Session, conv_id: int) -> int:
    rows = list(
        db.execute(
            select(ChatMessage.ord)
            .where(ChatMessage.conversation_id == conv_id)
            .order_by(ChatMessage.ord.desc())
            .limit(1)
        ).scalars()
    )
    return (rows[0] + 1) if rows else 0


def _own_card_or_404(db: Session, cid: int, card_id: int, user_id: int) -> tuple[Conversation, PlanCard]:
    conv = db.get(Conversation, cid)
    if conv is None or conv.user_id != user_id:
        raise HTTPException(404)
    card = db.get(PlanCard, card_id)
    if card is None or card.conversation_id != cid:
        raise HTTPException(404)
    return conv, card


@router.post("/{cid}/plan/{card_id}/reject", include_in_schema=False)
async def plan_reject(
    cid: int,
    card_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    conv, card = _own_card_or_404(db, cid, card_id, user.id)
    if card.status not in ("pending",):
        raise HTTPException(409, f"plan card is {card.status}")
    card.status = "rejected"
    card.approver_id = user.id
    card.approved_at = datetime.now(timezone.utc)
    try:
        args_dict = json.loads(card.args)
    except Exception:
        args_dict = {}
    rec_events.record_plan_card_decision(
        user.id,
        card.tool,
        "rejected",
        args_dict.get("host"),
        args_dict.get("command"),
    )
    db.add(
        AuditLog(
            actor_id=user.id,
            action="agent.plan.reject",
            target=f"plan_card:{card.id}",
            details=f"tool={card.tool}",
        )
    )
    return RedirectResponse(f"/chat/{cid}", status_code=303)


@router.post("/{cid}/plan/{card_id}/approve", include_in_schema=False)
async def plan_approve(
    cid: int,
    card_id: int,
    request: Request,
    edited_command: str = Form(""),
    confirmation: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    conv, card = _own_card_or_404(db, cid, card_id, user.id)
    if card.status != "pending":
        raise HTTPException(409, f"plan card is {card.status}")

    spec = get_action(card.tool)
    if spec is None:
        card.status = "failed"
        card.result = json.dumps({"ok": False, "summary": f"unknown tool {card.tool!r}"})
        db.add(AuditLog(actor_id=user.id, action="agent.plan.fail", target=f"plan_card:{card.id}", details="unknown tool"))
        db.commit()
        return RedirectResponse(f"/chat/{cid}", status_code=303)

    # Compute the effective args. Currently only ssh_exec exposes an editable
    # field; the form sends just `edited_command` + the existing args.
    try:
        args: dict = json.loads(card.args)
    except json.JSONDecodeError:
        args = {}
    if edited_command.strip():
        args["command"] = edited_command.strip()

    # Re-classify against the live policy; if elevated, require typed
    # confirmation matching the host name.
    if card.tool == "ssh_exec":
        tier = classify_command(cfg, args.get("command", ""))
        if tier == "elevated":
            host_name = args.get("host", "")
            if confirmation.strip() != host_name:
                card.tier = "elevated"
                # leave status pending so the UI re-renders the elevated form
                db.flush()
                return RedirectResponse(
                    f"/chat/{cid}?elevated_card={card.id}", status_code=303
                )
        card.tier = tier

    card.effective_args = json.dumps(args)
    card.status = "approved"
    card.approver_id = user.id
    card.approved_at = datetime.now(timezone.utc)
    plan_id = card.id
    plan_tool = card.tool
    rec_events.record_plan_card_decision(
        user.id,
        plan_tool,
        "approved",
        args.get("host"),
        args.get("command"),
    )
    db.add(
        AuditLog(
            actor_id=user.id,
            action="agent.plan.approve",
            target=f"plan_card:{plan_id}",
            details=f"tool={plan_tool} tier={card.tier}",
        )
    )
    # Commit the approval before running the action so the UI reflects state
    # even if execution takes a while.
    db.commit()

    # Execute. Any unhandled exception in the runner is converted to a
    # failed ActionResult so the conversation captures it instead of 500-ing.
    try:
        result = await spec.runner(cfg, args)
    except Exception as e:
        from dosm.agent.actions import ActionResult

        result = ActionResult(
            ok=False,
            summary=f"runner crashed: {type(e).__name__}: {e}",
            stderr=repr(e),
        )
    result_payload = result.to_dict()
    rec_events.record_plan_card_result(user.id, plan_tool, result.ok, result.summary)

    with session_scope() as s2:
        c2 = s2.get(PlanCard, plan_id)
        if c2 is not None:
            c2.result = json.dumps(result_payload)
            c2.status = "executed" if result.ok else "failed"
        # Append the tool result as a synthetic assistant turn so the LLM
        # sees it next time.
        next_ord = _next_ord(s2, cid)
        s2.add(
            ChatMessage(
                conversation_id=cid,
                role="assistant",
                content=f"[tool: {plan_tool}] {result.summary}\n\n"
                + (f"stdout:\n{result.stdout[:4000]}\n" if result.stdout else "")
                + (f"stderr:\n{result.stderr[:2000]}\n" if result.stderr else ""),
                citations=None,
                ord=next_ord,
            )
        )
        c = s2.get(Conversation, cid)
        if c is not None:
            c.updated_at = datetime.now(timezone.utc)
        s2.add(
            AuditLog(
                actor_id=user.id,
                action="agent.plan.execute",
                target=f"plan_card:{plan_id}",
                details=f"ok={result.ok} exit={result.exit_code} dur={result.duration_ms}ms",
            )
        )

    return RedirectResponse(f"/chat/{cid}", status_code=303)
