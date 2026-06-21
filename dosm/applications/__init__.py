"""Host organisation: a 3-tier application -> environment -> unit tree.

A neutral package name (the tree's root tier is "application") for the host
grouping feature. Distinct from ``dosm.org`` (the AD-backed people directory)
and from the documentation ``Folder`` taxonomy (table ``applications``).
"""
from __future__ import annotations

from dosm.applications.routes import router

__all__ = ["router"]
