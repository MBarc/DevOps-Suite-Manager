from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.agent.actions import classify_command, get_action
from dosm.auth.deps import require_user
from dosm.db import get_session, session_scope
from dosm.models import AuditLog, ChatMessage, Conversation, PlanCard, User
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


def _own_card_or_404(
    db: Session, cid: int, card_id: int, user_id: int
) -> tuple[Conversation, PlanCard]:
    conv = db.get(Conversation, cid)
    if conv is None or conv.user_id != user_id:
        raise HTTPException(404)
    card = db.get(PlanCard, card_id)
    if card is None or card.conversation_id != cid:
        raise HTTPException(404)
    return conv, card


def _exec_result_message(plan_tool: str, result) -> str:
    return (
        f"[tool: {plan_tool}] {result.summary}\n\n"
        + (f"stdout:\n{result.stdout[:4000]}\n" if result.stdout else "")
        + (f"stderr:\n{result.stderr[:2000]}\n" if result.stderr else "")
    )


@router.post("/{cid}/plan/{card_id}/reject", include_in_schema=False)
async def plan_reject(
    cid: int,
    card_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    conv, card = _own_card_or_404(db, cid, card_id, user.id)
    if card.status != "pending":
        return RedirectResponse(f"/chat/{cid}", status_code=303)
    card.status = "rejected"
    card.approver_id = user.id
    card.approved_at = datetime.now(timezone.utc)
    try:
        args_dict = json.loads(card.args)
    except Exception:
        args_dict = {}
    rec_events.record_plan_card_decision(
        user.id, card.tool, "rejected", args_dict.get("host"), args_dict.get("command")
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
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    form_data = await request.form()

    conv, card = _own_card_or_404(db, cid, card_id, user.id)
    if card.status != "pending":
        return RedirectResponse(f"/chat/{cid}", status_code=303)

    spec = get_action(card.tool)
    if spec is None:
        card.status = "failed"
        card.result = json.dumps({"ok": False, "summary": f"unknown tool {card.tool!r}"})
        db.add(
            AuditLog(
                actor_id=user.id,
                action="agent.plan.fail",
                target=f"plan_card:{card.id}",
                details="unknown tool",
            )
        )
        db.commit()
        return RedirectResponse(f"/chat/{cid}", status_code=303)

    try:
        args: dict = json.loads(card.args)
    except json.JSONDecodeError:
        args = {}

    # Apply operator edits from the form. If the operator types a value into
    # a secret field it overrides whatever the model provided. If they leave
    # it blank, the model-provided value (e.g. extracted from the user's own
    # message) is kept. Non-secret fields: form value wins when non-empty.
    for field_def in spec.args_schema:
        fname = field_def["name"]
        ftype = field_def.get("type", "string")
        form_val = str(form_data.get(f"arg_{fname}", "") or "")
        if form_val.strip():
            args[fname] = form_val.strip()

    # Classify tier. Command-execution tools use the config allow-list;
    # all others use the spec's own classify callable.
    _COMMAND_CLASSIFY_TOOLS = {"ssh_exec", "local_exec", "winrm_exec"}
    if card.tool in _COMMAND_CLASSIFY_TOOLS:
        tier = classify_command(cfg, args.get("command", ""))
    else:
        tier = spec.classify(args)

    # Elevated actions require typed confirmation of the designated field value.
    ecf = spec.elevated_confirm_field
    if tier == "elevated" and ecf:
        expected = str(args.get(ecf, ""))
        confirmation = str(form_data.get("confirmation", "") or "")
        if confirmation.strip() != expected:
            card.tier = "elevated"
            db.flush()
            return RedirectResponse(f"/chat/{cid}?elevated_card={card.id}", status_code=303)

    card.tier = tier
    card.effective_args = json.dumps(args)
    card.status = "approved"
    card.approver_id = user.id
    card.approved_at = datetime.now(timezone.utc)
    plan_id = card.id
    plan_tool = card.tool
    rec_events.record_plan_card_decision(
        user.id, plan_tool, "approved", args.get("host"), args.get("command")
    )
    db.add(
        AuditLog(
            actor_id=user.id,
            action="agent.plan.approve",
            target=f"plan_card:{plan_id}",
            details=f"tool={plan_tool} tier={card.tier}",
        )
    )
    db.commit()

    try:
        result = await spec.runner(cfg, args)
    except Exception as e:
        from dosm.agent.actions import ActionResult

        result = ActionResult(ok=False, summary=f"runner crashed: {type(e).__name__}: {e}", stderr=repr(e))

    result_payload = result.to_dict()
    rec_events.record_plan_card_result(user.id, plan_tool, result.ok, result.summary)

    with session_scope() as s2:
        c2 = s2.get(PlanCard, plan_id)
        if c2 is not None:
            c2.result = json.dumps(result_payload)
            c2.status = "executed" if result.ok else "failed"
        next_ord = _next_ord(s2, cid)
        s2.add(
            ChatMessage(
                conversation_id=cid,
                role="assistant",
                content=_exec_result_message(plan_tool, result),
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


@router.post("/{cid}/plan/approve_group", include_in_schema=False)
async def plan_approve_group(
    cid: int,
    request: Request,
    message_id: int = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    """Approve and execute all pending safe-tier plan cards for a given message."""
    cfg = request.app.state.config
    conv = db.get(Conversation, cid)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(404)

    pending_cards = list(
        db.execute(
            select(PlanCard)
            .where(
                PlanCard.conversation_id == cid,
                PlanCard.message_id == message_id,
                PlanCard.status == "pending",
                PlanCard.tier == "safe",
            )
            .order_by(PlanCard.id)
        ).scalars()
    )

    if not pending_cards:
        return RedirectResponse(f"/chat/{cid}", status_code=303)

    now = datetime.now(timezone.utc)
    approved: list[tuple[int, str, dict]] = []

    for card in pending_cards:
        spec = get_action(card.tool)
        if spec is None:
            card.status = "failed"
            card.result = json.dumps({"ok": False, "summary": f"unknown tool {card.tool!r}"})
            continue
        try:
            args: dict = json.loads(card.args)
        except json.JSONDecodeError:
            args = {}
        card.effective_args = json.dumps(args)
        card.status = "approved"
        card.approver_id = user.id
        card.approved_at = now
        db.add(
            AuditLog(
                actor_id=user.id,
                action="agent.plan.approve",
                target=f"plan_card:{card.id}",
                details=f"tool={card.tool} tier=safe group=true",
            )
        )
        approved.append((card.id, card.tool, args))

    db.commit()

    for plan_id, plan_tool, args in approved:
        spec = get_action(plan_tool)
        if spec is None:
            continue
        rec_events.record_plan_card_decision(
            user.id, plan_tool, "approved", args.get("host"), args.get("command")
        )
        try:
            result = await spec.runner(cfg, args)
        except Exception as e:
            from dosm.agent.actions import ActionResult

            result = ActionResult(ok=False, summary=f"runner crashed: {type(e).__name__}: {e}", stderr=repr(e))

        result_payload = result.to_dict()
        rec_events.record_plan_card_result(user.id, plan_tool, result.ok, result.summary)

        with session_scope() as s2:
            c2 = s2.get(PlanCard, plan_id)
            if c2 is not None:
                c2.result = json.dumps(result_payload)
                c2.status = "executed" if result.ok else "failed"
            next_ord = _next_ord(s2, cid)
            s2.add(
                ChatMessage(
                    conversation_id=cid,
                    role="assistant",
                    content=_exec_result_message(plan_tool, result),
                    citations=None,
                    ord=next_ord,
                )
            )
            conv_row = s2.get(Conversation, cid)
            if conv_row is not None:
                conv_row.updated_at = datetime.now(timezone.utc)
            s2.add(
                AuditLog(
                    actor_id=user.id,
                    action="agent.plan.execute",
                    target=f"plan_card:{plan_id}",
                    details=f"ok={result.ok} exit={result.exit_code} dur={result.duration_ms}ms group=true",
                )
            )

    return RedirectResponse(f"/chat/{cid}", status_code=303)
