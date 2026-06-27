"""Pipeline visibility helpers (shared vs private).

Mirrors ``dosm/credentials/access.py``: a pipeline is ``shared`` (visible to
everyone in the tenant) or ``private`` (visible only to its ``owner_id`` and
tenant admins). Single source of truth for "can this user see / run this
pipeline", reused by the pipelines list, detail/edit/delete routes (which 404
rather than 403 on invisible rows so a private pipeline's existence doesn't
leak), and the run guard.
"""
from __future__ import annotations

from sqlalchemy import or_

from dosm.auth.deps import user_has_role
from dosm.models import Pipeline, User


def _is_admin(user: User | None) -> bool:
    # tenant_admin OR platform_admin - both get the unrestricted visibility view
    # (still tenant-scoped separately via tenant_clause).
    return user_has_role(user, "tenant_admin")


def can_see_pipeline(user: User | None, pipeline: Pipeline) -> bool:
    """True if ``user`` may see ``pipeline`` (list it, open its detail page)."""
    if pipeline.visibility != "private":
        return True
    if _is_admin(user):
        return True
    return user is not None and pipeline.owner_id == user.id


def can_use_pipeline(user: User | None, pipeline: Pipeline) -> bool:
    """True if ``user`` may run ``pipeline``. Same rule as visibility today; kept
    distinct so the run-time policy can diverge later."""
    return can_see_pipeline(user, pipeline)


def visible_pipelines_filter(user: User | None):
    """A SQLAlchemy boolean clause restricting ``Pipeline`` rows to those
    ``user`` may see. Returns ``True`` (no restriction) for admins; ``None``
    callers should treat as no visibility restriction (e.g. agent/CLI)."""
    if _is_admin(user):
        return True
    owner_id = user.id if user is not None else None
    return or_(
        Pipeline.visibility != "private",
        Pipeline.owner_id == owner_id,
    )
