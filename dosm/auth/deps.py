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


# ---- Role-based access control -------------------------------------------
#
# A single, ranked role ladder is the source of truth for authorization.
# Higher rank implies every capability of the ranks below it. ``require_role``
# is the FastAPI-dependency factory that replaced the ``_require_admin`` body
# that used to be copy-pasted into every module; ``user_has_role`` is the plain
# predicate for places that can't use ``Depends`` (WebSocket handlers).

ROLE_RANK: dict[str, int] = {"viewer": 0, "operator": 1, "admin": 2}


def user_has_role(user: User | None, minimum: str) -> bool:
    """True if ``user`` holds at least ``minimum`` on the role ladder."""
    if user is None or not user.is_active:
        return False
    return ROLE_RANK.get(user.role, -1) >= ROLE_RANK[minimum]


def require_role(minimum: str):
    """Return a dependency that requires at least ``minimum`` role.

    Builds on ``require_user`` (so unauthenticated browsers still get the
    login redirect) and raises 403 when the role rank is insufficient.
    """
    if minimum not in ROLE_RANK:  # pragma: no cover - programmer error
        raise ValueError(f"unknown role: {minimum!r}")

    def _dep(user: User = Depends(require_user)) -> User:
        if ROLE_RANK.get(user.role, -1) < ROLE_RANK[minimum]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires {minimum} role",
            )
        return user

    return _dep


# Convenience dependencies for the common gates.
require_admin = require_role("admin")
require_operator = require_role("operator")


class _NotAuthenticated(HTTPException):
    def __init__(self, next_path: str):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
        self.next_path = next_path


def not_authenticated_exception_handler(request: Request, exc: _NotAuthenticated):
    target = f"/login?next={exc.next_path}" if exc.next_path and exc.next_path != "/" else "/login"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
