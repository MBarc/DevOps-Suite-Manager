"""Pipeline-payload visibility helpers (shared vs private).

Mirrors ``dosm/credentials/access.py``: a payload is ``shared`` (visible to
everyone who can run the pipeline) or ``private`` (visible only to its creator
and admins). This is the single source of truth for "can this user see this
payload", reused by the run-page picker, the payloads list, and the
edit/rename/copy/delete routes (which 404 rather than 403 on invisible rows, so
a private payload's existence doesn't leak).
"""
from __future__ import annotations

from sqlalchemy import or_

from dosm.auth.deps import user_has_role
from dosm.models import PipelinePayload, User


def _is_admin(user: User | None) -> bool:
    # admin OR platform_admin - both get the unrestricted visibility view
    # (payloads are tenant-scoped via their parent pipeline, not here).
    return user_has_role(user, "admin")


def can_see_payload(user: User | None, payload: PipelinePayload) -> bool:
    if payload.visibility != "private":
        return True
    if _is_admin(user):
        return True
    return user is not None and payload.created_by_id == user.id


def visible_payloads_filter(user: User | None):
    """A SQLAlchemy boolean clause restricting payloads to those ``user`` may
    see. Returns ``True`` (no restriction) for admins."""
    if _is_admin(user):
        return True
    owner_id = user.id if user is not None else None
    return or_(
        PipelinePayload.visibility != "private",
        PipelinePayload.created_by_id == owner_id,
    )
