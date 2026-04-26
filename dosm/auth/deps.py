from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from dosm.db import get_session
from dosm.models import User


def get_current_user(
    request: Request,
    db: Session = Depends(get_session),
) -> User | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        return None
    return user


def require_user(
    request: Request,
    user: User | None = Depends(get_current_user),
) -> User:
    if user is None:
        # For browser routes we'd rather redirect than 401; raise a special
        # exception that our login redirect handler catches.
        raise _NotAuthenticated(request.url.path)
    return user


class _NotAuthenticated(HTTPException):
    def __init__(self, next_path: str):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
        self.next_path = next_path


def not_authenticated_exception_handler(request: Request, exc: _NotAuthenticated):
    target = f"/login?next={exc.next_path}" if exc.next_path and exc.next_path != "/" else "/login"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
