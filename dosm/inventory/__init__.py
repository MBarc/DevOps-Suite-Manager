"""Inventory blade: hosts + pipelines + credentials in one org-tree explorer.

An experiment to see whether a single unified inventory view can replace the
separate Hosts / Pipelines / Credentials / File-transfer blades.
"""
from dosm.inventory.routes import router as inventory_router

__all__ = ["inventory_router"]
