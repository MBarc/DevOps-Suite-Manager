from __future__ import annotations

from datetime import datetime

from dosm.certs.routes import peek_cached as _routes_peek
from dosm.monitoring.adapters.base import CertInfo  # re-exported for any callers


def peek_cached() -> tuple[list[CertInfo], datetime] | None:
    return _routes_peek()
