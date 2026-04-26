from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.passwords import verify_password
from dosm.db import get_session
from dosm.models import AuditLog, User

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str = "/") -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request,
        "auth/login.html",
        {"error": None, "next": next, "username": ""},
    )


@router.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_session),
):
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return _templates(request).TemplateResponse(
            request,
            "auth/login.html",
            {"error": "Invalid credentials.", "next": next, "username": username},
            status_code=401,
        )
    request.session["user_id"] = user.id
    db.add(
        AuditLog(
            actor_id=user.id,
            action="auth.login",
            target=f"user:{user.id}",
            ip=request.client.host if request.client else None,
        )
    )
    return RedirectResponse(next or "/", status_code=303)


@router.post("/logout", include_in_schema=False)
async def logout(request: Request, db: Session = Depends(get_session)):
    uid = request.session.get("user_id")
    request.session.clear()
    if uid is not None:
        db.add(AuditLog(actor_id=uid, action="auth.logout", target=f"user:{uid}"))
    return RedirectResponse("/login", status_code=303)
