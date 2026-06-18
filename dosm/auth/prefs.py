"""Per-user UI preferences.

Each ``User`` carries a private JSON blob (``User.prefs_json``) of lightweight UI
preferences - last-used filters, default landing, etc. These are *personal*:
they're scoped to the one user and never shared, which is the distinction from
the admin-only global Settings page.

Keep prefs small and non-authoritative. Anything that gates access or changes
behaviour for other users belongs in config / the DB proper, not here.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from dosm.models import User


def get_prefs(user: User) -> dict[str, Any]:
    """Decode a user's preferences blob, tolerating null/garbage."""
    raw = user.prefs_json or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_pref(user: User, key: str, default: Any = None) -> Any:
    return get_prefs(user).get(key, default)


def set_pref(db: Session, user: User, key: str, value: Any) -> None:
    """Set a single preference and persist it. Caller's session is flushed but
    not committed (follows the request-session-owns-the-commit convention)."""
    prefs = get_prefs(user)
    if value is None:
        prefs.pop(key, None)
    else:
        prefs[key] = value
    user.prefs_json = json.dumps(prefs)
    db.add(user)
    db.flush()
